# Native map prop placement and instancing

This note records the placement model required for native Killzone level
imports and explains the apparent “one prop at world origin” result. It is a
research and implementation contract, not a claim that native level export is
safe.

Research snapshot:

- `D:\Github\Killzone_Blender` at
  `3005cb6c262f27c1310191dfe4362d03879de9a4` (`v0.13.0`), including the
  pre-existing working-tree state on 2026-10-23;
- `D:\Github\KillzoneRecomp-Extended` at
  `437e86b55aeaed14b05142cf35758c0e44b3faab`;
- the authorized NTSC-U retail corpus described by
  `D:\Github\KillzoneRecomp-Extended\docs\assetforms.md`;
- Blender 5.2.2 LTS for in-memory importer checks.

No importer code was changed for this investigation.

## Current implementation status

The findings in this research snapshot are implemented in Killzone Asset
Tools 0.13.1. Map import now emits direct-world roots and every authored
`StaticMeshInstance` and `CollisionMeshInstance` visual occurrence, applies
each serialized row-vector matrix once at the Blender boundary, and reuses
immutable mesh datablocks without collapsing occurrence objects. Collision
resource identities remain metadata; collision geometry is still unresolved.

The maintained implementation summary is in
[Architecture: Level graph](./architecture.md#92-level-graph), and current
corpus/import behavior is in
[Technical reference: Native level evidence](./technical-reference.md#native-level-evidence).
The remaining placement work concerns animated skinned instances,
proximity-parent composition, collision geometry, and runtime visibility/LOD
systems.

## Correction: collision-backed visual occurrences

The original investigation below correctly explains ordinary
`StaticMeshInstance` placement, but its direct-root rule was incomplete.
Follow-up testing on **1. Helghast Assault - HQ Grounds** identified a second
authored visual occurrence class: `CollisionMeshInstance`.

Each fixed-size `0x70` record contains both a collision-resource reference and
a `General.MeshResource` reference to the static visual graph, plus a
row-vector affine matrix. Across all 47 retail maps, 11,970 such records have
finite affine matrices and uniquely resolve both references. The importer now:

- expand and place every non-null `StaticMeshInstance` visual reference;
- expand and place every `CollisionMeshInstance.General.MeshResource`
  reference;
- exclude targets of both classes from the implicit direct-world roots; and
- preserve the collision resource identity as metadata without claiming that
  collision geometry itself has been reconstructed.

This is the root cause for the reported HQ Grounds definitions
`0x00546F8C`, `0x00553EDA`, and `0x00553FD4` appearing once at the origin.
They have 19, 30, and 9 collision-mesh placements respectively. Their visual
resources were previously mistaken for unreferenced direct-world roots.

## Evidence labels

- **Verified — corpus**: bounds-checked observations across the stated retail
  corpus, or matching executable control/data flow.
- **Verified — fixture**: reproduced from a named local retail Core/CoreStream
  pair with hashes below.
- **Verified — implementation**: direct code trace and an executed unit,
  Blender smoke, or in-memory import check.
- **Strong inference**: consistent with all evidence but lacking a direct
  declaration or final runtime trace.
- **Unknown**: must remain metadata/raw bytes and must not drive destructive
  conversion.

## Conclusion

The focused prop, regular static resource `0x060E4FAA` in
`level.11.sdplatform_hakha.core`, is **one geometry definition referenced by
18 distinct `StaticMeshInstance` records**. Those records have 18 distinct
directory identities, object IDs, and matrices. None of their translations is
the origin.

The one-copy result is reproduced only when that resource is loaded through
the **standalone static-model/definition path**:

- `build_static_model_render_plan()` deliberately creates one identity
  occurrence for the selected definition;
- it deliberately has no level-instance occurrences;
- a regular leaf therefore becomes one Blender object at identity.

That behavior is correct for a definition preview but is not a map placement
import. The existing diagnostic
`D:\Github\Killzone_Blender\probe-output\static-prop-sdplatform-hakha-060e4faa-diagnostics.json`
already records `level_occurrences: 18`; its associated `.blend` is a material
and geometry diagnostic, not a complete level-placement fixture.

The `StaticMeshInstance` path does not reproduce the focused Hakha defect. An
in-memory import of the hashed Hakha fixture created:

| Measure | Result |
|---|---:|
| Serialized static instances in the level | 35 |
| Blender static occurrence owners | 35 |
| Owners referencing `0x060E4FAA` | 18 |
| Distinct matrices for those owners | 18 |
| Owners at `(0, 0, 0)` | 0 |
| Blender mesh datablocks for those owners | 1 |

The last row is intentional instancing: 18 Blender objects share one immutable
mesh datablock. Counting meshes, resource tokens, or source names instead of
objects will incorrectly report one.

## Prioritized root-cause assessment

### 1. Definition import was mistaken for occurrence import — verified

This exactly explains the focused artifact.

`D:\Github\Killzone_Blender\io_scene_killzone\core\native_static_model.py`:

1. `parse_native_static_model()` selects one resource graph root.
2. It calls `build_static_model_render_plan()` in
   `core\native_level.py`.
3. That plan emits selected LOD0 leaves as `path="direct"` with the identity
   matrix and sets `instance_occurrences=()`.

`D:\Github\Killzone_Blender\io_scene_killzone\native_static_io.py` then creates
one object per selected leaf. For `0x060E4FAA`, the measured plan is one leaf,
eight packets, 224 triangles, one identity occurrence, and zero instance
occurrences.

By contrast, the map path starts at `KZ_OT_import_map.execute()` in
`io_scene_killzone\operators.py`, parses every compact instance record, and
passes all occurrences to `import_native_level_data()`.

The normal asset catalog also classifies `level.*.core` entries as maps and
then `continue`s; it does not expose their embedded roots as normal **Import
Model** choices. A level resource can reach the definition-only path through
a diagnostic or direct API call, but that output must not be used to judge
map placement.

### 2. Definition identity was mistaken for occurrence identity — verified

All 18 focused records have:

- the same resource token, `0x060E4FAA`;
- the same source name, `StaticMeshInstance`;
- one reusable regular-static definition.

They have 18 different compact-directory keys, object IDs, and matrices.
Any dictionary keyed by display name or resource token collapses valid
occurrences. Blender also suffixes duplicate object names (`.001`, `.002`,
and so on), while all objects may still share `obj.data`.

The current map importer uses
`NativeObjectKey(section_offset, object_index)` for occurrence grouping and
for the mesh cache key. That is the correct separation:

```text
definition identity = Core + resource directory key
occurrence identity = Core + compact-instance directory key
display name         = non-unique metadata
resource token       = Core-local reference, intentionally repeated
```

### 3. Untransposed source rows produce the exact origin symptom — conditional

Native instance translation is stored in the final **row** of a row-vector
matrix. Blender applies matrices to column vectors and reads object
translation from the final column. Passing the four serialized rows directly
to `object.matrix_world` therefore leaves Blender location at zero.

The current native map path is correct:
`native_level_io._object_matrix()` builds the source rows and calls
`.transposed()`. `tests\blender_smoke.py` verifies both translation and the
complete matrix.

This remains the first check for an older installed extension, a downstream
script, or a future refactor. The discriminating Blender-console test is:

```python
from mathutils import Matrix

src = Matrix((
    (1, 0, 0, 0),
    (0, 1, 0, 0),
    (0, 0, 1, 0),
    (10, 20, 30, 1),
))
assert tuple(src.translation) == (0.0, 0.0, 0.0)
assert tuple(src.transposed().translation) == (10.0, 20.0, 30.0)
```

If **Import Map** still yields one origin object, verify the active add-on is
`0.13.1`, verify that `KZ_OT_import_map` ran, and inspect the source-matrix
custom properties before changing parsing.

### 4. Object origin was mistaken for geometry location — verified

Identity placement is legal and has two distinct meanings:

- an explicit `StaticMeshInstance` can serialize an identity matrix;
- an explicit `CollisionMeshInstance` can serialize an identity matrix; and
- a resource graph root referenced by neither visual occurrence class is an
  implicit direct-world occurrence at identity.

Direct-world vertices are already decoded in level/world coordinates. Their
Blender object origin can be `(0, 0, 0)` while their bounds are hundreds of
units away. Existing Mountain Pass output contains 230 instance leaf objects
at an identity object matrix, but inspected bounds include centers such as
approximately `(124.74, 219.54, 68.08)` and
`(457.78, 172.26, 54.82)`. Do not invent a translation from a mesh centroid.

### 5. The project bundle path has no placement schema — conditional

`io_scene_killzone\scene_io.py::_create_mesh_object()` creates one new mesh
and object per version-1 `.kzm` asset and assigns no object transform.
The bundle manifest has no native instance graph. Bundle controls are
currently hidden and the format is explicitly project-owned, not a native
map format. It must not be used as an acceptance test for native level
placement.

### 6. Unsupported prop-like classes are omissions, not collapsed instances

`SimpleAnimatingSkinnedMeshInstance` objects are currently metadata-only
Empties. Physics objects, destructibles, pickups, particles, and other
gameplay objects can also look like “props” but have different schemas.
Their absence does not justify coercing them into `StaticMeshInstance`.

## End-to-end native map trace

The active static map path is:

```text
KZ_OT_import_map.execute
  -> native_level_io.import_native_level
  -> core.native_level.load_native_level
  -> KillzoneArchiveSource catalog lookup
  -> paired level Core + CoreStream reads
  -> parse_native_level
       -> parse_native_level_sections
       -> parse_native_static_resources
       -> parse_native_level_instances
       -> build_static_render_plan
       -> selected material/texture parsing
  -> native_level_io.import_native_level_data
       -> one cached Mesh per regular resource directory key
       -> one Blender occurrence owner per serialized instance
       -> one direct-world object per selected direct leaf
```

Important implementation locations are:

| Path | Symbol | Responsibility |
|---|---|---|
| `D:\Github\Killzone_Blender\io_scene_killzone\operators.py` | `KZ_OT_import_map` | Select the map workflow, not model preview |
| `...\core\core_format.py` | `parse_core_header` | Core magic, variant, string table, padding |
| `...\core\native_level.py` | `parse_native_core_sections` | Expanded/compact/BoneMap section discovery |
| same | `parse_native_static_resources` | Regular/Multi/LOD definition graph |
| same | `parse_native_level_instances` | Static and simple-skinned occurrence records |
| same | `build_static_render_plan` | Direct roots plus every explicit occurrence |
| `...\core\native_static_geometry.py` | `parse_regular_static_resource` | Resource stream ownership, chunks, packets, vertex decode |
| `...\native_level_io.py` | `_object_matrix` | Row-vector to Blender-column conversion |
| same | `_build_resource_mesh` | One leaf mesh with per-face material binding |
| same | `import_native_level_data` | Collections, objects, shared meshes, owner/child transforms |
| `...\core\native_static_model.py` | `parse_native_static_model` | Definition-only model preview |
| `...\native_static_io.py` | `import_native_static_model_data` | Identity-placed standalone definition |

## Container, section, and stream hierarchy

### Shipped containment

The verified retail containment is:

```text
ISO
  -> FILES.DAT catalog
  -> byte range in FILES03.DAT: level.*.core
  -> paired byte range in FILES04.DAT: level.*.corestream
  -> Core header and string table
  -> typed sections
  -> object directories
  -> object blobs and object-owned CoreStream ranges
```

All 47 retail level lumps are variant 5 Core/CoreStream pairs. Variant 1 is
self-contained and is not the spatial level format; the 41 variant-1
`*.missionscript.core` files are separate mission-script resources.

Every Core begins:

| File offset | Type | Meaning |
|---:|---|---|
| `0x00` | `char[16]` | `Core<1.79><PSX2>` |
| `0x10` | `u32` | Variant (`1` or `5`) |
| `0x14` | `u32` | String-table byte size |
| `0x18` | `u32` | String count, first string-table word |

Strings are length-prefixed, have zero padding to four bytes, and no null
terminator. The body begins at `0x18 + string_table_size`. Validated trailing
string-table padding is 0, 4, 8, or 12 zero bytes.

Every paired CoreStream begins with the 16-byte string
`<CoreStreamData>` followed by 48 zero bytes. Directory stream offsets are
relative to the start of the **complete** CoreStream, including this `0x40`
header.

### Expanded resource sections

For section start `S`, property count `P`, and object count `N`:

```text
S+0x00  u32 data_offset
S+0x04  u32 type_string_index
S+0x08  u32 object_count
S+0x0C  u32 inheritance_level_candidate
S+0x10  u32 property_count
S+0x14  property_descriptor[P]          # 16 bytes each
         u32 prefix_a = 1
         u32 prefix_b = 1
         expanded_entry[N]              # 32 bytes each
S+data_offset
         u32 zero_object_prefix
S+data_offset+4
         concatenated object blobs
```

The exact invariant is:

```text
data_offset = 0x1C + 0x10 * P + 0x20 * N
```

An expanded directory entry is:

| Entry offset | Type | Meaning |
|---:|---|---|
| `0x00` | `u32` | Object blob size |
| `0x04` | `u32` | Core-local object/reference token |
| `0x08` | `u32[4]` | Stable identity-like words; algorithm unknown |
| `0x18` | `u32` | Complete-CoreStream-relative owned offset |
| `0x1C` | `u32` | Owned stream byte size |

Regular resources own stream ranges. Multi and LOD wrappers use this directory
form but own zero stream bytes; their leaves retain stream ownership.

### Compact instance sections

Instances use a different directory and must not be parsed as expanded:

```text
S+0x00  u32 data_offset
S+0x04  u32 type_string_index
S+0x08  u32 object_count
S+0x0C  u32 inheritance_level_candidate
S+0x10  u32 property_count
S+0x14  property_descriptor[P]          # 16 bytes each
         u32 prefix_a = 0
         u32 prefix_b = 0
         compact_entry[N]               # {u32 blob_size, u32 object_id}
         [u32 zero, u32 zero]            # optional alignment, not an entry
S+data_offset
         u32 zero_object_prefix
S+data_offset+4
         concatenated object blobs
```

Define:

```text
base          = 0x1C + 0x10 * P + 0x08 * N
first_object  = align_up(S + base + 4, 0x10)
data_offset   = first_object - S - 4
```

Validated compact sections use `data_offset == base` or `base + 8`; any
optional eight bytes are zero alignment. The mandatory zero word remains at
`S + data_offset`. Compact entries have no identity words and no CoreStream
ownership.

The section header's `object_count` is the placement-record count. A Multi
part count, LOD count, packet count, or unique resource-token count is not an
instance count.

### Regular resources, chunks, and packet records

A regular static object's custom block starts at object-relative `+0x40`:

```text
+0x40  u32 chunk_count
       u32 chunk_size[chunk_count]
       align 4
       u32 mesh_record_count
       mesh_record[mesh_record_count]   # 24 bytes each
       u8 root_flags[2]
       align 4
       f32 position_decode_matrix[16]
       f32 position_scale[4]
```

Chunk sizes must sum exactly to the expanded directory entry's owned stream
size. The first owned chunk contains per-record packet offsets, raw packet
bounds, and chunk indices. Packet ranges are bounded by the declared chunks.
There is no placement transform in a stream chunk.

Each 24-byte mesh record contains its `Material` token at `+0x00`; packet
membership selects the record and therefore the Blender face material.
`StaticMeshInstance` contains no material override.

The resource matrix is a quantization/decode transform, not the placement
matrix. Its observed form is diagonal scale plus translation:

```text
sx  0   0   0
0   sy  0   0
0   0   sz  0
tx  ty  tz  1
```

The implemented vertex decode is:

```python
p_resource = (
    raw_x * sx + tx,
    raw_y * sy + ty,
    raw_z * sz + tz,
)
```

Only after this decode is an occurrence placement applied.

## Resource graph and references

### Wrapper layouts

`MultiStaticMeshResource` stores:

```text
+0x00  u32 part_count = N
+0x04..+0x27 inherited/visibility fields
+0x28  u32 part_token[N]
```

`LodStaticMeshResource` stores:

```text
+0x00  u32 mesh_count = N
+0x04  u32 lod_info_count = N
+0x08..+0x2B inherited/visibility fields
+0x2C  u32 mesh_token[N]
        f32 threshold[N]
```

Multi expands every selected child. LOD child zero is the baseline/default
import. Importing all LOD children creates overlapping alternatives, not more
authored prop copies. Threshold zero is exactly `0.0`; later thresholds are
finite, positive, and monotonic.

Wrapper relationships form an acyclic graph, not necessarily a tree. Shared
children and nested wrappers are legal. Use memoization plus recursion-stack
cycle detection, but do not deduplicate distinct graph paths into one render
occurrence.

### Reference rules

- Tokens are local to one Core.
- Resolve against the expected type, not merely the numeric value.
- Preserve exact directory identity after resolution.
- A bare object token is not a global primary key.
- Process every non-null instance target even when it is not a graph root.
  Mall and Docks1 each contain one legal instance target that is a wrapper
  child.
- The retail level corpus has one legal null static resource reference.

Across the 47 levels, static instance targets are:

| Target | Records |
|---|---:|
| `LodStaticMeshResource` | 14,642 |
| `RegularStaticMeshResource` | 7,013 |
| `MultiStaticMeshResource` | 106 |
| Null | 1 |
| **Total** | **21,762** |

## Instance record layouts

### `StaticMeshInstance`

All 21,762 retail static records are exactly `0x60` bytes:

| Blob offset | Type | Meaning |
|---:|---|---|
| `0x00` | `u32` | Name-is-identifier flag (`0` or `1`) |
| `0x04` | `u32` | Core string index for name |
| `0x08` | `u32` | Parent token; null in every retail record |
| `0x0C` | `f32[16]` | Row-major, row-vector affine placement |
| `0x4C` | `u32` | Nullable static resource token |
| `0x50` | `u32` | Opaque footer |
| `0x54` | `u8[0x0C]` | Zero trailer |

Observed footer values are `0x00000001`, `0x00010001`, `0x00020001`, and
`0x00030001`; their meaning is unknown.

### `SimpleAnimatingSkinnedMeshInstance`

All 656 retail simple-skinned records are exactly `0x70` bytes:

| Blob offset | Type | Meaning |
|---:|---|---|
| `0x00` | `u32` | Core string index for name |
| `0x04` | `u32` | Name-is-identifier flag (`0` or `1`) |
| `0x08` | `u32` | Nullable `ProximitySensorInstance` parent |
| `0x0C` | `f32[16]` | Row-major, row-vector affine placement |
| `0x4C` | `u32` | Skinned resource token |
| `0x50` | `u32` | `SkeletonAnimationResource` token |
| `0x54` | `u32` | Blend-shape animation token; retail value is null |
| `0x58` | `u32[2]` | Zero |
| `0x60` | `u32` | Opaque footer |
| `0x64` | `u8[0x0C]` | Zero trailer |

Do not share a byte-for-byte “instance header” parser: the first two static
and simple-skinned fields are reversed. All default-animation references
resolve. Of 656 records, 132 have a typed proximity-parent reference.

No serialized `SkinnedMeshInstance` or `MultiStaticMeshInstance` occurs in
the validated 612-Core non-mission-script corpus. Runtime class registration
alone is not permission to synthesize either serialized type.

## Transform contract

### Serialized order

All 22,418 static plus simple-skinned instance matrices have the source form:

```text
m00 m01 m02 0
m10 m11 m12 0
m20 m21 m22 0
tx  ty  tz  1
```

The parser must reject non-finite values and require exact affine sentinels at
flat indices 3, 7, 11, and 15: `0, 0, 0, 1`.

For a source row point:

```python
world[j] = matrix[12 + j] + sum(
    local[i] * matrix[4*i + j] for i in range(3)
)
```

This convention makes all 2,529 zone-linked static transformed AABBs overlap
their owning Zone. The corpus includes 200 nonuniformly scaled instances and
no observed shear, so preserving only location and rotation is still wrong.

### Blender conversion

With no host basis conversion:

```python
M_blender = M_source_rows.transposed()
```

If source vertices are converted by a homogeneous basis `C`, the occurrence
must use the same basis exactly once:

```python
M_blender = C @ M_source_rows.transposed() @ C.inverted()
```

Equivalent choices are:

1. convert every vertex and occurrence with `C`; or
2. retain source coordinates and put one conversion on a scene root.

Do not do both. Do not transpose twice. Multi/LOD wrappers add no transform;
an owner Empty applies the instance matrix once and its selected leaf children
remain identity-local.

### Handedness, axes, and units

| Claim | Status |
|---|---|
| Source placement uses row vectors with translation in the final row | **Verified — corpus** |
| Geometry/model space is right-handed, `+X` right, `+Z` up | **Strong inference**, supported by geometry, quaternion, determinant, and camera evidence |
| Native forward is `+Y` | **Strong inference** |
| One native unit is approximately one metre | **Strong inference** |
| Current native static importer uses `C = I`, scale `1` | **Verified — implementation** |
| The project-owned `.kzm` bundle declares right-handed, Z-up, `-Y`-forward metres | **Verified for the bundle only**, not a native declaration |

The data-faithful native choice is currently identity basis and unit scale.
If display convention should map inferred native `+Y` forward to Blender
`-Y`, a proper `RotZ(pi)` is sufficient; it is not a handedness reflection
and should be centralized rather than hidden in mesh or instance code.

The camera-side mapping `H(x,y,z)=(x,-z,y)` documented by the recomp research
is not a model-import basis.

### Parents and zones

- Static instance parent tokens are null.
- Zone membership is a typed membership/reference relation, not transform
  parenting. An instance translation can lie outside a Zone while its
  transformed bounds overlap the Zone.
- The 132 proximity-parented simple-skinned records remain unresolved:
  static evidence does not establish whether their stored child matrix is
  parent-local or already world-space. Preserve both values and do not create
  a Blender parent until a runtime composition trace establishes order.
- Core sections, stream chunks, Multi wrappers, and LOD wrappers do not add a
  hidden parent transform.

## Correct render and Blender object model

### Direct and explicit paths

Build incoming wrapper-edge counts over exact resource directory keys. A
resource with no incoming edge is a graph root.

```python
for instance in static_instances + collision_mesh_instances:
    if instance.resource is not null:
        for leaf in expand(instance.resource, multi="all", lod=0):
            emit_occurrence(
                leaf=leaf,
                occurrence_key=instance.directory_key,
                matrix=instance.matrix,
                path=instance.occurrence_path,
            )

for root in graph_roots:
    if root is not directly targeted by either occurrence class:
        for leaf in expand(root, multi="all", lod=0):
            emit_occurrence(
                leaf=leaf,
                occurrence_key=None,
                matrix=identity,
                path="direct",
            )
```

Do not require either visual target to be a root. Conversely, do not import
every regular leaf independently: that duplicates wrapper children and
alternate LODs. The collision resource referenced by a
`CollisionMeshInstance` is separate from its visible `MeshResource`; retaining
that token does not decode or import collision geometry.

Across all 47 levels:

| Measure | Count |
|---|---:|
| Regular static leaves | 9,327 |
| Multi-static wrappers | 137 |
| LOD-static wrappers | 2,793 |
| Resource roots | 5,896 |
| Roots referenced by static instances | 3,158 |
| Roots referenced by collision-mesh instances | 1,985 |
| Non-root static/collision visual targets | 2 / 3 |
| Visual targets shared by both classes | 0 |
| Direct-world roots | 753 |
| Direct LOD0 leaf occurrences | 813 |
| Static-instance LOD0 leaf occurrences | 22,109 |
| Collision-mesh-instance LOD0 leaf occurrences | 15,928 |
| Direct triangle occurrences | 291,081 |
| Static-instance triangle occurrences | 2,326,919 |
| Collision-mesh-instance triangle occurrences | 7,259,592 |

The earlier direct-root attribution for SDDocking resources `0x061005A8` and
`0x0615026C` was incorrect. They are not reachable from a
`StaticMeshInstance`, but they are referenced by `CollisionMeshInstance`;
their appearance in the GS trace supports the collision-backed visual path.

### Blender representation

Use:

- one reusable `bpy.types.Mesh` per exact regular-resource directory key;
- one distinct Blender object/owner per explicit instance directory key;
- one identity direct-world object per selected direct occurrence;
- an ordinary mesh object when one instance expands to one leaf;
- one owner Empty plus identity-local mesh children when an instance expands
  to multiple leaves;
- an Empty for a null-resource instance;
- metadata-only Empties for simple-skinned occurrences until their geometry,
  animation, and parent composition are connected.

For every Multi owner:

```text
owner.matrix_world = converted instance matrix
child.matrix_local = identity
child.data         = cached leaf mesh
```

Applying the same matrix to owner and child local transforms doubles the
placement. Applying it once per wrapper depth is also wrong.

Shared mesh data is not collapsed placement:

```text
18 objects -> 18 matrix_world values -> 1 obj.data
```

### Materials, LOD, and visibility

- A regular mesh record owns the Material token. The packet's
  `mesh_record_index` determines each face's material.
- There is no per-static-instance material token in the `0x60`-byte record,
  so sharing the exact leaf mesh and its material slots is appropriate.
- Current import selects LOD child zero. It preserves parsed thresholds but
  does not perform runtime camera-dependent LOD selection.
- Maximum-visible-distance fields, Zone/Portal visibility, runtime culling,
  and material mutation are not reconstructed. Current Blender visibility is
  therefore an import policy, not retail culling behavior.
- These limitations can show too much geometry; they do not turn 18 authored
  records into one occurrence.

## Concrete Hakha fixture

The fixture is:

```text
localcache.ps2/lumps/level.11.sdplatform_hakha.core
Core SHA-256:
  ed3d8011bcfaaa60b7d10951e6f2cd2d9e1e3b945c4a0c9bb5585778df6c108f
CoreStream SHA-256:
  3d28ac0c4cf5f0b6d094168ba909cb2846a83405f8da097a4a979c86c9bc7aeb
```

Offsets below are from the complete Core unless explicitly described as
body-relative. `NativeObjectKey.section_offset` is body-relative.

| Item | Value |
|---|---|
| Core body file offset | `0x39B0` |
| `StaticMeshInstance` section body offset | `0x17E8B0` |
| Same section complete-file offset | `0x182260` |
| Property count / object count | 5 / 35 |
| Compact base / `data_offset` | `0x184` / `0x18C` |
| Optional alignment | 8 zero bytes |
| First object body offset | `0x17EA40` |
| Focus resource directory key | `(section 0x182DE0, index 30)` |
| Focus resource stream span | offset `0x6EA000`, size `0x1700` |
| Focus resource packets / triangles | 8 / 224 |
| Focus references | 18 |

The first focused occurrence is compact key
`(section 0x17E8B0, index 8)`, object ID `0x060EF87A`, with its `0x60`-byte
blob at complete-file offset `0x1826F0`. It has:

```text
name index      = 437
name            = "StaticMeshInstance"
identifier flag = 0
parent          = 0
resource        = 0x060E4FAA
footer          = 0x00020001

source rows =
[-0.6554610133, -0.0600482076,  0.0,           0.0]
[ 0.0,           0.0000000109, -0.6582059264, 0.0]
[ 0.0900723189, -0.9831916094,  0.0000000164, 0.0]
[448.4928588867, 50.6394844055,  6.5271806717, 1.0]
```

The other 17 translations form two more nearby groups around X approximately
421 and 392. All 18 complete matrices are distinct and all 18 object IDs and
directory keys are distinct.

The same input intentionally produces different plans depending on API:

| API | Direct leaves | Instance leaves | Placement result |
|---|---:|---:|---|
| `load_native_static_model(..., root_token=0x060E4FAA)` | 1 | 0 | One definition at identity |
| `load_native_level(...)` | 57 | 46 total | Complete map graph |
| Filtered map occurrences for `0x060E4FAA` | 0 direct | 18 | 18 authored placements |

## Verified facts, strong inferences, and unknowns

### Verified

- Variant-5 level containment, Core/CoreStream framing, expanded and compact
  section formulas, prefixes, object zero words, and observed alignment.
- Fixed instance blob sizes and field offsets.
- All retail instance matrices are finite row-vector affine matrices beginning
  at `+0x0C`.
- Static resource, default-animation, and proximity-parent type relationships
  stated above.
- Static parent nullness, zero trailers, blend-shape nullness, and observed
  footer domains.
- Multi-all and LOD0 graph expansion required for a non-duplicated default
  import.
- Direct-world roots are required in addition to explicit instances.
- `0x060E4FAA` has 18 authored placements and one reusable definition.
- Current map importer creates all 18 focused owners with one shared mesh and
  correct non-origin matrices.
- Current standalone static-model importer creates one identity definition
  occurrence by design.

### Strong inferences

- Right-handed source model space, `+X` right, `+Z` up.
- Native `+Y` forward.
- Approximately one native unit per metre.
- The static position decode's final rounding details; its scale/translation
  equation matches the complete measured corpus within documented
  quantization bounds.

### Unknown or deliberately unresolved

- Meaning of instance footer bits.
- Exact algorithm/semantic name of expanded-entry identity words.
- Exact semantic name of the section header's inheritance-like word and some
  property descriptor fields.
- Runtime LOD metric and its physical units.
- Full maximum-distance, Zone/Portal/PVS, and runtime visibility behavior.
- Whether a proximity-parented simple-skinned matrix is local or world-space,
  and the parent composition order.
- Complete placement behavior of other gameplay “prop-like” classes.
- Retail variants outside the validated NTSC-U build and known beta layout.
- Safe native serialization/export.

## Implementation contract and remaining work

The current v0.13.1 static **map** path follows the complete direct, static,
and collision-backed visual model. P0-P4 below are implemented regression
contracts rather than pending work. P5 remains evidence-gated future work.

### P0 — make the workflow unambiguous

1. Treat **Import Map**, standalone **Import Model**, diagnostic definition
   previews, and `.kzm` bundle import as distinct products.
2. A definition-only result should state that no level placement context was
   loaded. It must not be labelled a map prop placement import.
3. Record add-on version, operator, Core path/hash, resource key, serialized
   instance count, occurrence count, and unique mesh count in every placement
   diagnostic.
4. Diagnose objects by custom directory-key properties, never by object name.

Acceptance for the focus fixture is 18 owners, 18 matrices, zero origin
owners, and one mesh datablock.

### P1 — retain strict parser framing

1. Keep compact and expanded section parsing separate.
2. Validate prefixes, `data_offset`, optional alignment, mandatory zero word,
   blob bounds, fixed instance sizes, trailers, and affine sentinels.
3. Preserve raw blobs, exact 16 source floats, footer, string index/flag,
   object ID, section offset, and object index.
4. Resolve tokens through a same-Core, expected-type multimap. Fail on missing
   or ambiguous non-null references.
5. Keep the one legal null static reference as an explicit empty occurrence.

### P2 — retain graph/occurrence separation

1. Key graph nodes by exact directory identity.
2. Expand all Multi children and only LOD child zero for the default import.
3. Allow an instance to start at any resolved node, including the two known
   child targets.
4. Emit once for every static or collision-mesh instance record and every
   selected visual graph path; never deduplicate by resource token, name, or
   leaf mesh.
5. Emit graph roots targeted by neither occurrence class at identity and do
   not independently emit every stored regular leaf.

### P3 — centralize transform conversion

1. Keep static-resource quantization decode separate from occurrence
   placement.
2. Preserve the source row matrix and convert once at the Blender boundary.
3. Define one explicit basis `C`; retain `C=I`, scale 1 until a product
   decision requires a display basis.
4. Apply an instance matrix once at the occurrence owner. Keep Multi children
   identity-local.
5. Do not create Blender parent relationships from Zone membership or
   unresolved proximity references.

### P4 — preserve sharing and binding

1. Cache immutable leaf meshes by exact resource directory key.
2. Create one object/owner per authored occurrence.
3. Bind materials per mesh record/face. Do not invent instance material
   overrides.
4. Keep explicit identity instances distinguishable from implicit direct
   roots in names and custom properties.

### P5 — add later systems without changing the static contract

1. Connect simple-skinned instances to the standalone skinned model and
   animation graph only after parent composition is proven.
2. Preserve LOD thresholds and maximum-distance/visibility metadata before
   attempting live Blender culling.
3. Add other gameplay classes under their own schemas, not under a generic
   fabricated prop record.

## Test plan and fixture expectations

### Asset-free unit tests

| Test | Required assertion |
|---|---|
| Compact section with no alignment | `data_offset == base`; first blob starts after mandatory zero word |
| Compact section with alignment | exactly eight zero alignment bytes accepted |
| Wrong compact prefix | `(1,1)` rejected for an instance section |
| Static fixed blob | `0x60` accepted with flag then name index |
| Collision-mesh fixed blob | `0x70` preserves both references and its affine matrix |
| Simple-skinned fixed blob | `0x70` accepted with name index then flag |
| Matrix translation | source final row `(10,20,30,1)` becomes Blender location `(10,20,30)` |
| Full affine preservation | nonuniform scale and rotation survive without decomposition |
| Duplicate name/token | two instance keys produce two objects sharing one mesh |
| Multi instance | one owner, all selected child objects identity-local |
| Nested/shared wrappers | every legal path emits an occurrence; one leaf mesh remains shared |
| LOD wrapper | child zero only; alternate children do not overlap |
| Child target | instance expansion succeeds without requiring a graph root |
| Explicit identity vs direct root | two distinct occurrence kinds remain distinguishable |
| Collision visual target | authored matrix used and target omitted from direct roots |
| Null resource | one owner Empty, no mesh child |
| Cycle | recursion-stack validation rejects it |

Existing targeted tests:

```powershell
Set-Location 'D:\Github\Killzone_Blender'
python -m unittest `
  tests.test_native_level.NativeStaticGraphTests `
  tests.test_native_level.NativeLevelInstanceTests
```

On the research snapshot this ran five tests successfully.

Existing Blender smoke coverage validates matrix transpose, owner/child
composition, duplicate names, shared mesh identity, direct-root distinction,
null resources, and simple-skinned metadata:

```powershell
Set-Location 'D:\Github\Killzone_Blender'
& 'D:\Blender Foundation\Blender 5.2\blender.exe' `
  --background --factory-startup `
  --python '.\tests\blender_smoke.py'
```

This passed on Blender 5.2.2 LTS.

### Retail read-only fixtures

The Hakha hashed fixture must assert:

```text
35 serialized static instances
46 selected instance leaf occurrences
61 serialized collision-mesh instances
61 selected collision-mesh leaf occurrences
12 selected direct leaf occurrences
18 records targeting 0x060E4FAA
18 distinct focused instance keys/object IDs/matrices
0 focused translations at origin
1 focused regular-resource directory key
8 focused packets
224 focused stored triangles
18 Blender occurrence owners
1 shared Blender mesh datablock
```

Additional regression expectations:

| Fixture/corpus | Expected |
|---|---|
| Street2 | 71 static leaves, 237 collision leaves, 7 direct leaves; 60,477, 116,298, and 3,561 triangles respectively |
| Mountain Pass | 675 static leaves, 212 collision leaves, 20 direct leaves; 99,545, 106,666, and 7,878 triangles respectively |
| Full 47 levels | 21,762 static records, 11,970 collision records, 22,109 static leaves, 15,928 collision leaves, 813 direct leaves |
| Full 47 levels | 2,326,919 static, 7,259,592 collision, and 291,081 direct triangle occurrences |
| Mall and Docks1 | both legal non-root instance targets imported |
| Full corpus | one null static reference preserved |
| HQ Grounds collision visuals | `0x00546F8C`, `0x00553EDA`, and `0x00553FD4` have 19, 30, and 9 transformed owners |
| Zone-linked subset | all 2,529 transformed static AABBs overlap their owning Zone |

### Required failure cases

Reject or explicitly preserve, as appropriate:

- truncated Core/CoreStream/string/section/directory/object spans;
- unsupported Core variant for a native level;
- non-zero CoreStream reserved prefix bytes;
- impossible section `data_offset`, non-zero alignment, or missing zero object
  prefix;
- wrong fixed instance size, invalid name flag/index, non-UTF-8 required name,
  non-zero trailer, or invalid affine sentinels;
- non-finite matrix elements;
- non-null static parent;
- missing, ambiguous, or wrong-type resource/default-animation/parent token;
- zero-part Multi, missing child, non-zero tail, graph cycle;
- mismatched LOD counts, non-zero first threshold, non-finite/non-positive or
  non-monotonic later thresholds;
- stream span overflow, chunk-sum mismatch, packet outside its owned chunk;
- any Blender result whose authored occurrence count is lower than the
  serialized count because names, tokens, or mesh datablocks were used as
  occurrence keys.

Do not “repair” a rejected transform by moving geometry to a bounding-box
center. Report the exact directory key and raw matrix instead.

## Reproducible focused diagnostics

Set an authorized source locally, then inspect the definition/occurrence
distinction without writing extracted game data:

```powershell
Set-Location 'D:\Github\Killzone_Blender'
$env:KZ_SOURCE = '<authorized ISO or directory containing all DAT files>'
@'
import os
from io_scene_killzone.core.native_level import load_native_level
from io_scene_killzone.core.native_static_model import load_native_static_model

path = "localcache.ps2/lumps/level.11.sdplatform_hakha.core"
token = 0x060E4FAA
level = load_native_level(os.environ["KZ_SOURCE"], path, include_textures=False)
focus = [i for i in level.instances if i.resource_token == token]

print("records / keys / matrices:",
      len(focus), len({i.key for i in focus}), len({i.matrix for i in focus}))
print("translations:", [i.matrix[12:15] for i in focus])
print("raw first blob:", focus[0].raw_blob.hex())

model = load_native_static_model(
    os.environ["KZ_SOURCE"], path, root_token=token
)
print("definition direct / instance:",
      len(model.render_plan.direct_occurrences),
      len(model.render_plan.instance_occurrences))
'@ | python -
```

Expected key lines are:

```text
records / keys / matrices: 18 18 18
definition direct / instance: 1 0
```

After a complete map import, this Blender-console query distinguishes objects
from shared mesh data:

```python
focus = [
    obj for obj in bpy.data.objects
    if obj.get("killzone_resource_token") == "0x060E4FAA"
]
(
    len(focus),
    len({obj.data.as_pointer() for obj in focus if obj.type == "MESH"}),
    len({
        tuple(round(v, 6) for row in obj.matrix_world for v in row)
        for obj in focus
    }),
    sum(obj.matrix_world.translation.length < 1e-6 for obj in focus),
)
# expected: (18, 1, 18, 0)
```

Useful per-object inspection fields are:

```text
killzone_serialized_type
killzone_object_id
killzone_instance_section_offset
killzone_instance_object_index
killzone_resource_token
killzone_resource_section_offset
killzone_resource_object_index
killzone_source_matrix_f32
killzone_source_matrix_f32_hex
killzone_occurrence_kind
killzone_explicit_instance
```

At collection level, compare serialized counts to Blender owner counts rather
than only visible mesh datablocks:

```text
killzone_static_prop_count
killzone_animated_prop_count
killzone_null_static_prop_count
killzone_direct_root_count
killzone_unique_leaf_mesh_count
```

## Primary local evidence

- `D:\Github\KillzoneRecomp-Extended\docs\assetforms.md`
  - sections 2.1-2.3: Core/CoreStream framing and variants;
  - sections 4.2-4.3: expanded directories, ownership, and references;
  - sections 4.8-4.9: static vertex decode and embedded resources;
  - sections 4.15-4.17: instance layouts, matrices, Blender conversion,
    Multi, and LOD;
  - sections 5.1-5.6: level set, compact directories, zones, and render graph;
  - section 10 Level validation gates.
- `D:\Github\Killzone_Blender\io_scene_killzone\core\native_level.py`
  - current strict parsers and render-plan construction.
- `D:\Github\Killzone_Blender\io_scene_killzone\native_level_io.py`
  - current Blender sharing and occurrence implementation.
- `D:\Github\Killzone_Blender\probe-output\static-prop-sdplatform-hakha-060e4faa-diagnostics.json`
  - definition diagnostic explicitly recording 18 level occurrences.
- `D:\Github\Killzone_Blender\probe-output\level4-street2-complete-static-diagnostics.json`
  and
  `D:\Github\Killzone_Blender\probe-output\level10-mountainpass-complete-static-diagnostics.json`
  - independent direct/instance occurrence and triangle counts.
- `D:\Github\Killzone_Blender\tests\test_native_level.py` and
  `D:\Github\Killzone_Blender\tests\blender_smoke.py`
  - graph, layout, matrix, sharing, and Blender object checks.
