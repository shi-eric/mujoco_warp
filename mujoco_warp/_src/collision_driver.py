# Copyright 2025 The Newton Developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from typing import Any

import warp as wp

from .collision_convex import convex_narrowphase
from .collision_hfield import hfield_midphase
from .collision_primitive import primitive_narrowphase
from .collision_sdf import sdf_narrowphase
from .math import upper_tri_index
from .types import MJ_MAXVAL
from .types import BroadphaseType
from .types import Data
from .types import DisableBit
from .types import GeomType
from .types import Model
from .warp_util import event_scope

wp.set_module_options({"enable_backward": False})


@wp.func
def _sphere_filter(
  # Model:
  geom_rbound: wp.array2d(dtype=float),
  geom_margin: wp.array2d(dtype=float),
  # Data in:
  geom_xpos_in: wp.array2d(dtype=wp.vec3),
  geom_xmat_in: wp.array2d(dtype=wp.mat33),
  # In:
  geom1: int,
  geom2: int,
  worldid: int,
) -> bool:
  margin1 = geom_margin[worldid, geom1]
  margin2 = geom_margin[worldid, geom2]
  pos1 = geom_xpos_in[worldid, geom1]
  pos2 = geom_xpos_in[worldid, geom2]
  size1 = geom_rbound[worldid, geom1]
  size2 = geom_rbound[worldid, geom2]

  bound = size1 + size2 + wp.max(margin1, margin2)
  dif = pos2 - pos1

  if size1 != 0.0 and size2 != 0.0:
    # neither geom is a plane
    dist_sq = wp.dot(dif, dif)
    return dist_sq <= bound * bound
  elif size1 == 0.0:
    # geom1 is a plane
    xmat1 = geom_xmat_in[worldid, geom1]
    dist = wp.dot(dif, wp.vec3(xmat1[0, 2], xmat1[1, 2], xmat1[2, 2]))
    return dist <= bound
  else:
    # geom2 is a plane
    xmat2 = geom_xmat_in[worldid, geom2]
    dist = wp.dot(-dif, wp.vec3(xmat2[0, 2], xmat2[1, 2], xmat2[2, 2]))
    return dist <= bound


@wp.func
def _add_geom_pair(
  # Model:
  geom_type: wp.array(dtype=int),
  nxn_pairid: wp.array(dtype=int),
  # Data in:
  nconmax_in: int,
  # In:
  geom1: int,
  geom2: int,
  worldid: int,
  nxnid: int,
  # Data out:
  collision_pair_out: wp.array(dtype=wp.vec2i),
  collision_hftri_index_out: wp.array(dtype=int),
  collision_pairid_out: wp.array(dtype=int),
  collision_worldid_out: wp.array(dtype=int),
  ncollision_out: wp.array(dtype=int),
):
  pairid = wp.atomic_add(ncollision_out, 0, 1)

  if pairid >= nconmax_in:
    return

  type1 = geom_type[geom1]
  type2 = geom_type[geom2]

  if type1 > type2:
    pair = wp.vec2i(geom2, geom1)
  else:
    pair = wp.vec2i(geom1, geom2)

  collision_pair_out[pairid] = pair
  collision_pairid_out[pairid] = nxn_pairid[nxnid]
  collision_worldid_out[pairid] = worldid

  # Writing -1 to collision_hftri_index_out[pairid] signals
  # hfield_midphase to generate a collision pair for every
  # potentially colliding triangle
  if type1 == int(GeomType.HFIELD.value) or type2 == int(GeomType.HFIELD.value):
    collision_hftri_index_out[pairid] = -1


@wp.func
def _binary_search(values: wp.array(dtype=Any), value: Any, lower: int, upper: int) -> int:
  while lower < upper:
    mid = (lower + upper) >> 1
    if values[mid] > value:
      upper = mid
    else:
      lower = mid + 1

  return upper


@wp.kernel
def _sap_project(
  # Model:
  geom_rbound: wp.array2d(dtype=float),
  geom_margin: wp.array2d(dtype=float),
  # Data in:
  geom_xpos_in: wp.array2d(dtype=wp.vec3),
  # In:
  direction_in: wp.vec3,
  # Data out:
  sap_projection_lower_out: wp.array2d(dtype=float),
  sap_projection_upper_out: wp.array2d(dtype=float),
  sap_sort_index_out: wp.array2d(dtype=int),
):
  worldid, geomid = wp.tid()

  xpos = geom_xpos_in[worldid, geomid]
  rbound = geom_rbound[worldid, geomid]

  if rbound == 0.0:
    # geom is a plane
    rbound = MJ_MAXVAL

  radius = rbound + geom_margin[worldid, geomid]
  center = wp.dot(direction_in, xpos)

  sap_projection_lower_out[worldid, geomid] = center - radius
  sap_projection_upper_out[worldid, geomid] = center + radius
  sap_sort_index_out[worldid, geomid] = geomid


@wp.kernel
def _sap_range(
  # Model:
  ngeom: int,
  # Data in:
  sap_projection_lower_in: wp.array2d(dtype=float),
  sap_projection_upper_in: wp.array2d(dtype=float),
  sap_sort_index_in: wp.array2d(dtype=int),
  # Data out:
  sap_range_out: wp.array2d(dtype=int),
):
  worldid, geomid = wp.tid()

  # current bounding geom
  idx = sap_sort_index_in[worldid, geomid]

  upper = sap_projection_upper_in[worldid, idx]

  limit = _binary_search(sap_projection_lower_in[worldid], upper, geomid + 1, ngeom)
  limit = wp.min(ngeom - 1, limit)

  # range of geoms for the sweep and prune process
  sap_range_out[worldid, geomid] = limit - geomid


@wp.kernel
def _sap_broadphase(
  # Model:
  ngeom: int,
  geom_type: wp.array(dtype=int),
  geom_rbound: wp.array2d(dtype=float),
  geom_margin: wp.array2d(dtype=float),
  nxn_pairid: wp.array(dtype=int),
  # Data in:
  nworld_in: int,
  nconmax_in: int,
  geom_xpos_in: wp.array2d(dtype=wp.vec3),
  geom_xmat_in: wp.array2d(dtype=wp.mat33),
  sap_sort_index_in: wp.array2d(dtype=int),
  sap_cumulative_sum_in: wp.array(dtype=int),
  # In:
  nsweep_in: int,
  # Data out:
  collision_pair_out: wp.array(dtype=wp.vec2i),
  collision_hftri_index_out: wp.array(dtype=int),
  collision_pairid_out: wp.array(dtype=int),
  collision_worldid_out: wp.array(dtype=int),
  ncollision_out: wp.array(dtype=int),
):
  worldgeomid = wp.tid()

  nworldgeom = nworld_in * ngeom
  nworkpackages = sap_cumulative_sum_in[nworldgeom - 1]

  while worldgeomid < nworkpackages:
    # binary search to find current and next geom pair indices
    i = _binary_search(sap_cumulative_sum_in, worldgeomid, 0, nworldgeom)
    j = i + worldgeomid + 1

    if i > 0:
      j -= sap_cumulative_sum_in[i - 1]

    worldid = i // ngeom
    i = i % ngeom
    j = j % ngeom

    # get geom indices and swap if necessary
    geom1 = sap_sort_index_in[worldid, i]
    geom2 = sap_sort_index_in[worldid, j]

    # find linear index of (geom1, geom2) in upper triangular nxn_pairid
    if geom2 < geom1:
      idx = upper_tri_index(ngeom, geom2, geom1)
    else:
      idx = upper_tri_index(ngeom, geom1, geom2)

    if nxn_pairid[idx] < -1:
      worldgeomid += nsweep_in
      continue

    if _sphere_filter(
      geom_rbound,
      geom_margin,
      geom_xpos_in,
      geom_xmat_in,
      geom1,
      geom2,
      worldid,
    ):
      _add_geom_pair(
        geom_type,
        nxn_pairid,
        nconmax_in,
        geom1,
        geom2,
        worldid,
        idx,
        collision_pair_out,
        collision_hftri_index_out,
        collision_pairid_out,
        collision_worldid_out,
        ncollision_out,
      )

    worldgeomid += nsweep_in


def _segmented_sort(tile_size: int):
  @wp.kernel
  def segmented_sort(
    # Data in:
    sap_projection_lower_in: wp.array2d(dtype=float),
    sap_sort_index_in: wp.array2d(dtype=int),
  ):
    worldid = wp.tid()

    # Load input into shared memory
    keys = wp.tile_load(sap_projection_lower_in[worldid], shape=tile_size, storage="shared")
    values = wp.tile_load(sap_sort_index_in[worldid], shape=tile_size, storage="shared")

    # Perform in-place sorting
    wp.tile_sort(keys, values)

    # Store sorted shared memory into output arrays
    wp.tile_store(sap_projection_lower_in[worldid], keys)
    wp.tile_store(sap_sort_index_in[worldid], values)

  return segmented_sort


@event_scope
def sap_broadphase(m: Model, d: Data):
  """Broadphase collision detection via sweep-and-prune."""

  nworldgeom = d.nworld * m.ngeom

  # TODO(team): direction

  # random fixed direction
  direction = wp.vec3(0.5935, 0.7790, 0.1235)
  direction = wp.normalize(direction)

  wp.launch(
    kernel=_sap_project,
    dim=(d.nworld, m.ngeom),
    inputs=[
      m.geom_rbound,
      m.geom_margin,
      d.geom_xpos,
      direction,
    ],
    outputs=[
      d.sap_projection_lower,
      d.sap_projection_upper,
      d.sap_sort_index,
    ],
  )

  if m.opt.broadphase == int(BroadphaseType.SAP_TILE):
    wp.launch_tiled(
      kernel=_segmented_sort(m.ngeom),
      dim=(d.nworld),
      inputs=[d.sap_projection_lower, d.sap_sort_index],
      block_dim=m.block_dim.segmented_sort,
    )
  else:
    wp.utils.segmented_sort_pairs(
      d.sap_projection_lower,
      d.sap_sort_index,
      nworldgeom,
      d.sap_segment_index,
    )

  wp.launch(
    kernel=_sap_range,
    dim=(d.nworld, m.ngeom),
    inputs=[
      m.ngeom,
      d.sap_projection_lower,
      d.sap_projection_upper,
      d.sap_sort_index,
    ],
    outputs=[
      d.sap_range,
    ],
  )

  # scan is used for load balancing among the threads
  wp.utils.array_scan(d.sap_range.reshape(-1), d.sap_cumulative_sum, True)

  # estimate number of overlap checks
  # assumes each geom has 5 other geoms (batched over all worlds)
  nsweep = 5 * nworldgeom
  wp.launch(
    kernel=_sap_broadphase,
    dim=nsweep,
    inputs=[
      m.ngeom,
      m.geom_type,
      m.geom_rbound,
      m.geom_margin,
      m.nxn_pairid,
      d.nworld,
      d.nconmax,
      d.geom_xpos,
      d.geom_xmat,
      d.sap_sort_index,
      d.sap_cumulative_sum,
      nsweep,
    ],
    outputs=[
      d.collision_pair,
      d.collision_hftri_index,
      d.collision_pairid,
      d.collision_worldid,
      d.ncollision,
    ],
  )


@wp.kernel
def _nxn_broadphase(
  # Model:
  geom_type: wp.array(dtype=int),
  geom_rbound: wp.array2d(dtype=float),
  geom_margin: wp.array2d(dtype=float),
  nxn_geom_pair: wp.array(dtype=wp.vec2i),
  nxn_pairid: wp.array(dtype=int),
  # Data in:
  nconmax_in: int,
  geom_xpos_in: wp.array2d(dtype=wp.vec3),
  geom_xmat_in: wp.array2d(dtype=wp.mat33),
  # Data out:
  collision_pair_out: wp.array(dtype=wp.vec2i),
  collision_hftri_index_out: wp.array(dtype=int),
  collision_pairid_out: wp.array(dtype=int),
  collision_worldid_out: wp.array(dtype=int),
  ncollision_out: wp.array(dtype=int),
):
  worldid, elementid = wp.tid()

  # check for valid geom pair
  if nxn_pairid[elementid] < -1:
    return

  geom = nxn_geom_pair[elementid]
  geom1 = geom[0]
  geom2 = geom[1]

  if _sphere_filter(
    geom_rbound,
    geom_margin,
    geom_xpos_in,
    geom_xmat_in,
    geom1,
    geom2,
    worldid,
  ):
    _add_geom_pair(
      geom_type,
      nxn_pairid,
      nconmax_in,
      geom1,
      geom2,
      worldid,
      elementid,
      collision_pair_out,
      collision_hftri_index_out,
      collision_pairid_out,
      collision_worldid_out,
      ncollision_out,
    )


@event_scope
def nxn_broadphase(m: Model, d: Data):
  """Broadphase collision detection via brute-force search."""

  if m.nxn_geom_pair.shape[0]:
    wp.launch(
      _nxn_broadphase,
      dim=(d.nworld, m.nxn_geom_pair.shape[0]),
      inputs=[
        m.geom_type,
        m.geom_rbound,
        m.geom_margin,
        m.nxn_geom_pair,
        m.nxn_pairid,
        d.nconmax,
        d.geom_xpos,
        d.geom_xmat,
      ],
      outputs=[
        d.collision_pair,
        d.collision_hftri_index,
        d.collision_pairid,
        d.collision_worldid,
        d.ncollision,
      ],
    )


@event_scope
def collision(m: Model, d: Data):
  """Collision detection."""

  d.ncollision.zero_()
  d.ncon.zero_()
  d.ncon_hfield.zero_()
  d.collision_hftri_index.zero_()

  if d.nconmax == 0:
    return

  dsbl_flgs = m.opt.disableflags
  if (dsbl_flgs & DisableBit.CONSTRAINT) | (dsbl_flgs & DisableBit.CONTACT):
    return

  if m.opt.broadphase == int(BroadphaseType.NXN):
    nxn_broadphase(m, d)
  else:
    sap_broadphase(m, d)

  # Process heightfield collisions
  if m.nhfield > 0:
    hfield_midphase(m, d)

  # TODO(team): we should reject far-away contacts in the narrowphase instead of constraint
  #             partitioning because we can move some pressure of the atomics
  # TODO(team) switch between collision functions and GJK/EPA here
  convex_narrowphase(m, d)
  primitive_narrowphase(m, d)

  if m.has_sdf_geom:
    sdf_narrowphase(m, d)
