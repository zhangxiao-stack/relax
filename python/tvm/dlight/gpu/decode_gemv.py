# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""A rule for DecodeGEMV."""
from typing import List, Optional, Set, Tuple, Union

from tvm import arith, ir, tir
from tvm.target import Target

from ..base import (
    BlockInfo,
    ScheduleRule,
    normalize_prim_func,
    try_inline_contiguous_spatial,
)
from . import utils


def _get_reduction_expr(block: tir.Block) -> Optional[tir.PrimExpr]:
    # Detect and return `Y` in `X[...] = X[...] + Y`
    buffer_store = block.body
    if not isinstance(buffer_store, tir.BufferStore):
        return None
    if not isinstance(buffer_store.value, tir.Add):
        return None
    if not ir.structural_equal(
        buffer_store.value.a,
        tir.BufferLoad(buffer_store.buffer, block.body.indices),
        map_free_vars=True,
    ):
        return None
    return buffer_store.value.b


def _collect_vars_used_in_access_region(region: List[ir.Range]) -> Set[tir.Var]:
    tir_vars: Set[tir.Var] = set()

    def _collect_tir_var(expr):
        if isinstance(expr, tir.Var):
            tir_vars.add(expr)

    for expr in region:
        assert expr.extent == 1
        tir.stmt_functor.post_order_visit(expr.min, _collect_tir_var)
    return tir_vars


def _detect_dominant_read(block: tir.Block) -> tir.PrimExpr:
    dominant_read = None
    num_read_iters = -1
    for buffer_region in block.reads:
        tir_vars = _collect_vars_used_in_access_region(buffer_region.region)
        if num_read_iters < len(tir_vars):
            num_read_iters = len(tir_vars)
            dominant_read = buffer_region
    assert dominant_read is not None
    (result,) = dominant_read.buffer.offset_of([e.min for e in dominant_read.region])
    return result


def _is_broadcast_epilogue(
    sch: tir.Schedule,
    block: tir.schedule.BlockRV,
    epilogue: tir.schedule.BlockRV,
) -> bool:
    write_buffers = {r.buffer for r in sch.get(block).writes}
    epilogue_iters = {i.var: i for i in sch.get(epilogue).iter_vars if i.dom != 1}
    for buffer_region in sch.get(epilogue).reads:
        if buffer_region.buffer not in write_buffers:
            continue
        tir_vars = _collect_vars_used_in_access_region(buffer_region.region)
        if len(tir_vars) < len(epilogue_iters):
            return True
    return False


class DecodeGEMV(ScheduleRule):
    """A rule for DecodeGEMV."""

    def apply(  # pylint: disable=too-many-locals,too-many-branches,too-many-return-statements
        self,
        func: tir.PrimFunc,
        target: Target,
        _: bool,
    ) -> Union[None, tir.Schedule, List[tir.Schedule]]:
        if not isinstance(func, tir.PrimFunc):
            return None
        sch = tir.Schedule(func)
        block_infos = normalize_prim_func(sch)
        if block_infos is None:
            return None
        block_infos = try_inline_contiguous_spatial(sch, block_infos)
        if len(block_infos) == 1:
            epilogue = None
        elif len(block_infos) == 2:
            epilogue = block_infos[1]
            if not epilogue.is_injective():
                return None
        else:
            return None

        block_info = block_infos[0]
        block = block_info.block_rv
        block_stmt = sch.get(block)

        # Step 1. Check reduction block
        if (
            (not block_info.is_reduction())
            or len(block_stmt.writes) != 1
            or _get_reduction_expr(block_stmt) is None
        ):
            return None
        # Step 2. Normalize the block, merge spatial and reduction iters
        is_inner_reduction, c_factor = self._normalize(
            sch,
            block_info,
            arith.normalize_to_iter_sum(
                _detect_dominant_read(block_stmt),
                input_iters={i.var: i.dom for i in block_stmt.iter_vars},
            ),
        )
        if is_inner_reduction is None and c_factor is None:
            return None
        # Step 3. Do the scheduling
        if is_inner_reduction:
            self._sch_inner_reduction(sch, target, block, c_factor, epilogue)
        else:
            self._sch_inner_spatial(sch, target, block, c_factor, epilogue)
        return sch

    def _normalize(  # pylint: disable=too-many-branches
        self,
        sch: tir.Schedule,
        block_info: BlockInfo,
        access: arith.IterSumExpr,
    ) -> Tuple[Optional[bool], Optional[int]]:
        if access.base != 0:
            return None, None
        iter_to_info = {i.var: i for i in block_info.iters}
        s_loops, r_loops, c_loops, c_factor = [], [], [], None
        for split_expr in access.args:
            var = split_expr.source.source
            info = iter_to_info.pop(var)
            loop = info.loop_rv
            is_inner_reduction = info.kind == "R"
            if split_expr.lower_factor > 1:
                if c_loops:
                    return None, None
                loop, c_loop = sch.split(loop, factors=[None, split_expr.lower_factor])
                c_loops.append(c_loop)
                if not is_inner_reduction:
                    c_factor = split_expr.lower_factor
            if is_inner_reduction:
                r_loops.append(loop)
            else:
                s_loops.append(loop)

        if iter_to_info:
            for var, info in iter_to_info.items():
                if info.kind == "S" and info.dom == 1:
                    s_loops.append(info.loop_rv)
                else:
                    return None, None
        assert s_loops
        assert r_loops
        if len(s_loops) != len([i for i in block_info.iters if i.kind == "S"]):
            return None, None
        if not c_loops:
            c_loops = [sch.add_unit_loop(block_info.block_rv)]
        sch.reorder(*s_loops, *r_loops, *c_loops)
        sch.fuse(*s_loops)
        sch.fuse(*r_loops)
        return is_inner_reduction, c_factor

    def _sch_inner_reduction(  # pylint: disable=too-many-arguments
        self,
        sch: tir.Schedule,
        target: Target,
        block: tir.schedule.BlockRV,
        unroll_spatial_factor: Optional[int],
        epilogue_info: Optional[BlockInfo],
    ):
        # pylint: disable=invalid-name
        _, r, _ = sch.get_loops(block)
        (len_tx,) = utils.suggest_threads_per_block(  # pylint: disable=unbalanced-tuple-unpacking
            target, [sch.get(r)]
        )

        _, tx = sch.split(r, factors=[None, len_tx])
        # Schedule the RF block
        rf = sch.rfactor(tx, 0)
        bx, r, tx, _ = sch.get_loops(rf)
        sch.reorder(bx, tx, r)
        sch.bind(bx, "blockIdx.x")
        sch.bind(tx, "threadIdx.x")
        sch.set_scope(rf, 0, "local")
        sch.decompose_reduction(rf, r)
        # Schedule the write back block
        sch.reverse_compute_at(block, bx, preserve_unit_loops=True)
        _, tx, *s = sch.get_loops(block)
        s = sch.fuse(*s)
        sch.reorder(s, tx)
        if unroll_spatial_factor:
            s, inner = sch.split(s, factors=[None, unroll_spatial_factor])
            sch.reorder(s, tx, inner)
        sch.bind(tx, "threadIdx.x")
        # Schedule epilogue
        if epilogue_info is not None:
            epilogue = epilogue_info.block_rv
            sch.reverse_compute_at(epilogue, bx)
            if _is_broadcast_epilogue(sch, block, epilogue):
                sch.set_scope(block, 0, "shared")
                _, *s = sch.get_loops(epilogue)  # pylint: disable=invalid-name
                _, tx = sch.split(sch.fuse(*s), factors=[None, len_tx])
                sch.bind(tx, "threadIdx.x")
            else:
                sch.set_scope(block, 0, "local")
        # pylint: enable=invalid-name

    def _sch_inner_spatial(
        self,
        sch: tir.Schedule,
        _: Target,
        block: tir.schedule.BlockRV,
        unroll_spatial_factor: Optional[int],
        epilogue_info: Optional[BlockInfo],
    ):
        # pylint: disable=invalid-name
        s, r, _ = sch.get_loops(block)
        len_tx, len_ty = 16, 16
        _, _ = sch.split(s, factors=[None, len_tx])
        _, ty = sch.split(r, factors=[None, len_ty])
        # Schedule the RF block
        rf = sch.rfactor(ty, 0)
        bx, tx, r, ty, _ = sch.get_loops(rf)
        sch.reorder(bx, tx, ty, r)
        sch.bind(tx, "threadIdx.x")
        sch.bind(ty, "threadIdx.y")
        sch.bind(bx, "blockIdx.x")
        sch.set_scope(rf, 0, "local")
        sch.decompose_reduction(rf, r)
        # Schedule the write back block
        sch.reverse_compute_at(block, bx, preserve_unit_loops=True)
        _, r, *s = sch.get_loops(block)
        s = sch.fuse(*s)
        sch.reorder(s, r)
        if unroll_spatial_factor:
            s, inner = sch.split(s, factors=[None, unroll_spatial_factor])
            sch.reorder(s, r, inner)
        sch.bind(s, "threadIdx.x")
        sch.bind(r, "threadIdx.y")
        # Schedule epilogue
        if epilogue_info is not None:
            epilogue = epilogue_info.block_rv
            sch.reverse_compute_at(epilogue, bx)
            if _is_broadcast_epilogue(sch, block, epilogue):
                sch.set_scope(block, 0, "shared")
                _, *s = sch.get_loops(epilogue)  # pylint: disable=invalid-name
                _, tx, ty = sch.split(sch.fuse(*s), factors=[None, len_tx, len_ty])
                sch.bind(tx, "threadIdx.x")
                sch.bind(ty, "threadIdx.x")
            else:
                sch.set_scope(block, 0, "local")
        # pylint: enable=invalid-name
