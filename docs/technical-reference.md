# Killzone Asset Tools Technical Reference

[Back to the project overview](../README.md) |
[Architecture](./architecture.md)

This document records the measured native-format contracts, corpus results,
runtime evidence, importer policies, diagnostic commands, and unresolved
boundaries behind Killzone Asset Tools. Commands are shown relative to the
repository root.

The project targets **Blender 5.2.x** and keeps Blender-facing code separate
from game-format parsers.

## Implementation coverage

- Import and export version 1 Killzone Asset Bundles.
- Static triangle meshes, split normals, UVs, vertex colors, material slots,
  and JSON material metadata.
- Strict manifest validation, bounded binary parsing, SHA-256 verification,
  and bundle path containment.
- Byte-identical export of an unchanged interchange mesh.
- Byte-identical passthrough of an attached original game blob.
- Preservation of unknown interchange chunks when an edited mesh is exported.
- Read-only ISO 9660 discovery, hashing, and extraction of the game's five
  native data containers.
- Strict parsing and deterministic JSON reporting of the native `FILES.DAT`
  name and entry catalog.
- Strict, read-only import of the 19 proven self-contained native background
  textures from an authorized ISO or extracted DAT directory.
- Strict, read-only experimental import of the default-LOD static render graph
  from all 47 native level Core/CoreStream pairs.
- Strict parsing of all 13,891 variant-5 Texture definitions and 13,718
  Core-local logical tokens across all 111 Texture-bearing Cores, plus all 85
  level AnimatedTextures and the 11,870 Materials selected by the default-LOD
  static scene.
- Structural native Material assignment in Blender with variable-length layer
  parsing; exact `INDEX_4`, `INDEX_8`, `ALPHA_4`, and `ALPHA_8` indexed
  decoding; packed `RGBA_8888` and `RGB_888`; and explicit unresolved nodes
  for dynamic GLOW palettes and invalid resources.
- Purpose-built preview scenes add a labeled decoded-color diagnostic after
  exact `WRITE` and bounded V4-8 `ALPHAADD` admission fail; normal imports
  retain the evidence-gated disconnected behavior.
- Corpus-verified DMA/VIF decoding of 9,327 regular static resources and
  164,630 packets across all 18 packet-tail shapes, including signed
  positions, packed normals/ADC, UV0/UV1, V4-8 vertex data, and preserved
  terminal auxiliary bytes.
- Independently reproduced format evidence for generic Core typed sections,
  object-owned CoreStream ranges, material references, 29,680 skinned DMA/VIF
  packets, exact skinned quantization, direct selector-to-bone lookup, packed
  Skeleton hierarchy, and live matrix-palette construction.
- Strict, read-only standalone skinned-model import with Multi expansion,
  LOD0 selection, packed Skeleton parsing, one source-order bone influence at
  weight `1.0` per target, armature modifiers, selected native Materials, and
  packed decoded textures. Serialized temporary transforms are consumed as
  global inverse-bind matrices and inverted to form the Blender rest pose.
- Strict packed-animation block and track parsing with all nine time modes,
  exact fixed/adaptive translation decoding, exact scale decoding, bounded
  decoding for all quaternion codecs, retail VU0 approximate interpolation,
  fixed-axis angle wrapping, BoneMap/clip object loading, explicit Skeleton
  binding, native direct-local pose evaluation, and Blender 5.2 slotted Action
  baking with decomposition residual checks.
- Strict parsing of all 11 shared `DecalResource` definitions, plus pure
  helpers for the proven projector footprints, capacity policy, fade/alpha
  updates, ring submission, queue keys, and sort-descriptor packing.
- Runtime-verified static dual-UV routing, cubic-light vertex RGB, and exact GS
  state for one attribution-bounded three-pass material family.
- Corpus-verified decoding of 9,327 regular static resources and 164,630
  packets across all 47 level Cores, including all 18 packet-tail shapes.
- A verified level render-resource graph with 2,738 identity-placed direct
  world roots in addition to ordinary `StaticMeshInstance` draws.
- Headless tests against Blender 5.2.

The bundle coordinate convention is right-handed, Z-up, -Y-forward, in meters.
The `.kzm` file is a versioned interchange format owned by this project; it is
not claimed to be a native Killzone file format.

## Important native-format boundary

The native catalog, Core header, generic typed-section framing, object
directories, object-owned CoreStream ranges, standalone and embedded Texture
layouts, regular static material bindings, embedded static meshes, and regular
skinned packet topology are now measured. The implemented add-on imports the
proven standalone textures and the complete default-LOD static render graph
from all 47 level Cores. The workspace also exposes a standalone read-only
skinned-model and animation importer. Static geometry is split into
identity-placed direct-world and matrix-placed instance collections, with
shared Blender meshes for repeated leaves. Selected native Materials are
assigned per face; their serialized layers and texture dependencies are
represented as inspectable nodes.

The skinned geometry kernel, quantization, normals, one-weight transform-group
semantics, direct selector-to-source-bone lookup, Skeleton hierarchy, and live
pose-matrix palette construction are now instruction- and corpus-verified.
Multi/LOD wrappers, placement instances, BoneMaps, and animation-track
boundaries are also bounded. Packed-track framing, time selection, translation,
scale, every rotation codec, retail quaternion interpolation, and category
scatter are implemented. The importer enforces the retail selector domains,
12/24/6 Hz variable-time mapping, big-endian sparse translation, adaptive
descriptor bounds, and the measured `R=8`, `R=9`, and `P=5` invariants.
Variant-5 clip fields, BoneMaps, direct Q/QT/QTS locals, hierarchy
accumulation, inverse-bind deformation, and Blender Action conversion follow
the corpus/runtime contract documented by the recomp project.
All 11 shared decal policies are preserved from
`custom.materialeffects.core`, but decal marks, receiver groups, and generated
vertices are runtime state rather than serialized level objects. Native level
imports therefore do not fabricate projected decals. Static card-like meshes
in a level import are not evidence of the runtime decal system.
Exact static VU processing, native coordinate and front-face conventions,
generated coordinate equations, optional serialized transform multiplication,
full material composition beyond the bounded runtime capture, the artistic
source of dynamic GLOW palettes, the semantic role of serialized temporary
transforms, collision relationships, and general edited-object serialization
remain unresolved. The standalone skinned importer treats the temporary
transforms as row-vector global inverse-bind matrices, transposes and inverts
them for a candidate Blender rest pose, and records that interpretation on the
armature; this is a diagnostic conversion rather than a claim about the final
runtime bind equation. The static importer uses diagnostic object colors and a
normal-guided winding correction. Proven direct UV routes are linked to
inspection nodes, but native texture layers remain disconnected from final
shader output; this does not claim reconstructed final game materials or a
proven front-face convention. This add-on does **not** invent missing
conversions, animation data, or native encoders.

An original blob referenced by a bundle is verified and copied unchanged. If a
mesh is edited, export writes the edited `.kzm` and marks the original blob as
`preserved-original-not-reencoded`. A verified native adapter must be added
before an edited game blob can be produced.

## Verified native container evidence

The local NTSC-U executable contains the path `cdrom0:FILES.DAT`. The recomp's
evidence log identifies all offline game data containers as:

- `FILES.DAT`
- `FILES01.DAT`
- `FILES02.DAT`
- `FILES03.DAT`
- `FILES04.DAT`

The native catalog contains 8,704 `.mss` entries and no `.zone` or `.col`
entries. The latter names therefore remain runtime or resource-class
hypotheses rather than independently cataloged asset files.

Inventory an authorized disc image without extracting its contents:

```powershell
python .\tools\kz_asset_probe.py 'D:\private\Killzone.iso' `
  --hash `
  --output '.\probe-output\disc-report.json'
```

To make private working copies of only the five containers:

```powershell
python .\tools\kz_asset_probe.py 'D:\private\Killzone.iso' `
  --extract-to 'D:\private\Killzone-containers' `
  --output '.\probe-output\disc-report.json'
```

The probe validates ISO 9660 redundant endian fields, extents, paths, record
counts, directory depth, and sector boundaries. Its report includes sizes,
locations, prefixes, sample entropy, and optional SHA-256 hashes. Game data,
disc images, and default probe output are excluded by [.gitignore](../.gitignore)
and must not be packaged with the add-on.

### Native catalog

Validate the archive catalog without extracting game data:

```powershell
python .\tools\kz_catalog.py 'D:\private\Killzone.iso' `
  --output '.\probe-output\catalog-summary.json'
```

Add `--full` to include all file and directory records. The parser validates:

- the fixed header, zero-reserved fields, and bounded metadata size;
- the null-terminated ASCII name table;
- every recursive 12-byte directory/file record exactly once;
- package selection from bits 23-27;
- every offset and size against its referenced DAT container;
- declared file count, case-colliding paths, cycles, orphan records, and
  unknown file flags.

The authorized NTSC-U catalog used for development validates with:

| Measurement | Value |
|---|---:|
| Files | 11,569 |
| Directories | 1,163 |
| Name table | 199,408 bytes |
| Entry table | 152,788 bytes |
| `.mss` records | 8,704 |
| `.dat` records | 1,595 |
| `.core` records | 653 |
| `.corestream` records | 514 |
| `.pss` records | 66 |

All `.mss` records begin with `MCSS`, all `.core` records begin with
`Core<1.79><PSX2>`, and all `.corestream` records begin with
`<CoreStreamData>`. The two nonzero catalog header words at offsets `0x08` and
`0x0C`, plus directory flag bits beyond the directory marker, remain explicitly
unknown.

### Core resources

Validate every Core header without extracting game data:

```powershell
python .\tools\kz_core_scan.py 'D:\private\Killzone.iso' `
  --full `
  --output '.\probe-output\core-scan-full.json'
```

The authorized NTSC-U catalog validates all 653 Core resources:

| Variant | Core files | Paired `.corestream` |
|---:|---:|---:|
| 1 | 139 | 0 |
| 5 | 514 | 514 |

The string table stores a count followed by length-prefixed, four-byte-aligned
byte strings. Some non-leading symbols are not UTF-8, so the parser preserves
raw bytes as authoritative and only exposes optional UTF-8 text. The first
string names the first serialized type, not a dependable file-wide root type.
Several compound lumps begin with whichever object happened to be serialized
first. The observed leading serialized types are:

| Leading serialized type | Count |
|---|---:|
| `SkeletonAnimationResource` | 199 |
| `AITalkStateImmediate` | 101 |
| `KillZoneScript` | 95 |
| `SoundResource` | 95 |
| `Texture` | 48 |
| `RadioMessage` | 46 |
| `WeaponPickupResource` | 42 |
| `Font` | 12 |
| `RegularStaticMeshResource` | 7 |
| Other measured leading types | 8 |

These counts classify only the first serialized object and are not object or
mesh counts. Generic typed sections can now locate many later objects, but
unclassified sections and unknown object fields remain opaque.

### Generic Core object directories

A validated typed section starts with five `u32` values followed by its
property descriptors, a fixed `{1, 1}` prefix, and 32-byte object-directory
entries:

```text
u32 data_offset
u32 type_string_index
u32 object_count
u32 inheritance_level_candidate
u32 property_count
property_descriptor property[property_count]  # 16 bytes each
u32 prefix_a                                   # 1
u32 prefix_b                                   # 1
object_directory_entry object[object_count]    # 32 bytes each
serialized object blobs
```

The strict framing invariant is:

```text
data_offset = 0x1C + 0x10 * property_count + 0x20 * object_count
```

Each directory entry declares the serialized blob size, a Core-local object
ID/reference token, four stable identity words, and an absolute CoreStream
offset and size. The declared stream range is the object's ownership bound;
DMA-looking signatures outside it must not be consumed. Object IDs are scoped
to a Core and are not safe global primary keys.

The retail string tables contain regular-mesh class names in 102 Cores: 101
paired variant-5 model-bearing Cores and one variant-1 frontend Core containing
the names as symbols only. Strictly parsed objects in the paired set total:

| Object type | Objects | Mesh records | Packets | Stored triangles |
|---|---:|---:|---:|---:|
| `RegularStaticMeshResource` | 9,461 | 20,848 | 165,781 | 4,604,047 |
| `RegularSkinnedMeshResource` | 1,244 | 2,386 | 29,680 | 1,029,286 |
| `Skeleton` | 416 | | | |
| `Material` | 16,854 | | | |

These totals span all 101 paired model-bearing Cores. The separate strict
level-only pass covers the 47 level Cores and validates 9,327 of those regular
static resources, 164,630 packets, and 4,567,004 stored triangles.

All 23,234 regular-mesh record tokens resolve to `Material` objects in the
same Core. Of the 1,244 regular skinned objects, 1,233 reference a same-Core
`Skeleton` and 11 carry a null Skeleton token.

### Native static-mesh packet evidence

The seven files whose leading type is `RegularStaticMeshResource` share one
identical leading-object schema. Runtime loader traces and all seven authorized
samples establish the following bounded, read-only layout. This small leading
corpus remains the standalone scanner's focused subset and the reference
corpus for raw static vertex decoding; the level importer applies the same
validated invariants to every regular static resource in the 47 level Cores.

Every CoreStream has a 64-byte header containing `<CoreStreamData>` and 48 zero
reserved bytes. The leading static-mesh custom data declares one or more
object-owned stream chunk sizes followed by an array of 24-byte mesh records:

```text
+0x00  u32     Material object ID/reference token
+0x04  u32     emitted triangle count
+0x08  u16     packet count
+0x0A  u8      unknown flag; 1 in all nine records
+0x0B  u8      attribute flags; bit 0x10 enables V4-8
+0x0C  s16[3]  quantized record minimum
+0x12  s16[3]  quantized record maximum
```

For each record, the first stream chunk contains parallel packet-directory
arrays:

```text
u32    packet_relative_offset[packet_count]
s16    packet_bounds[packet_count][6]
u8     packet_chunk_index[packet_count]
```

The loader relocates each offset against its selected stream chunk. All 17
measured offsets select chunk zero, are unique, are 16-byte aligned, and remain
inside the declared chunk. Each directory bound exactly matches the extrema of
that packet's decoded positions. Each record bound exactly matches the
aggregate extrema of its packets.

Inspect the implemented directory and bounds scanner without extracting game
data:

```powershell
python .\tools\kz_static_mesh_scan.py 'D:\private\Killzone.iso' `
  --output '.\probe-output\static-mesh-scan-full.json'
```

The leading validation corpus contains:

| Measurement | Value |
|---|---:|
| Leading resources | 7 |
| Mesh records | 9 |
| DMA/VIF packets | 17 |
| Strip vertices | 709 |
| ADC-clear emitted triangles | 415 |
| Packets with optional V4-8 | 3 |

Each packet is a VIF1 DMA `RET` subchain with this fixed preamble:

```text
+0x00  0x60000000 | QWC
+0x04  0x80000000
+0x08  0x01000404        STCYCL 4,4
+0x0C  0x6C02C1D9        UNPACK V4-32, count 2
```

The transfer ends at `packet_offset + 0x10 + QWC * 0x10`. Bytes from that
endpoint to the next directory-bounded packet are zero allocation padding.
The two unpacked header vectors describe the vertex count, aligned attribute
sizes, and VU addresses:

```text
header_a = {vertex_count, 3 * vertex_count,
            3 * align_up(vertex_count, 4), end_of_attribute_arrays}
header_b = {position_addr, normal_adc_addr, color_or_uv_addr, uv_addr}
```

`position_addr` is `0xB7` in every sample. Static attribute streams use the
following command forms, with every payload aligned to four bytes. The
original 17-packet corpus uses V2-16 only; the full level corpus also uses
V4-16 and optional terminal byte-packed commands:

| VIF command | Per vertex | Interpretation |
|---|---:|---|
| `0x69 UNPACK V3-16` | 6 bytes | signed quantized XYZ |
| `0x6F UNPACK V4-5` | 2 bytes | packed normal XYZ plus ADC |
| `0x6E UNPACK V4-8` | 4 bytes | optional unsigned vertex diffuse RGBA |
| `0x65 UNPACK V2-16` | 4 bytes | signed 4.12 UV0 |
| `0x6D UNPACK V4-16` | 8 bytes | variant carrying signed 4.12 UV0 and UV1 |

The strict all-level pass validates 9,327 regular static resources, 164,630
physical packets, and 4,567,004 physically stored triangles. All packets obey:

```text
V3-16 position
V4-5  normal/ADC
[ V4-8 unsigned color ]
{ V2-16 | V4-16 }*
[ V1-8 | V2-8 | V3-8 ]
```

This produces 18 packet-tail shapes. Only 65,884 packets end in V2-16;
requiring that original-corpus layout incorrectly rejects the other 98,746.
The dominant shape, present in 67,038 packets, is
`V3-16,V4-5,V4-8,V4-16`. Zero coordinate commands are legal for generated
coordinate material paths. The optional terminal byte-packed stream follows
the coordinate arrays, but its renderer semantics remain unknown and its raw
component width and bytes must be preserved.

In the original 17-packet corpus, zero-valued VIF NOP padding after the aligned
UV payload is absent in eight packets, eight bytes in seven, and twelve bytes
in two. The last transfer word of every packet is `0x6501C1EE`, a one-element
V2-16 unpack to VU address `0x1EE`; its data is supplied by the caller outside
the `RET` packet. These packets are therefore callable subchains rather than
self-contained VIF programs.

The former 64-byte opaque block is a 4x4 position scale/translation matrix, and
the following 16-byte block contains `{sx, sy, sz, 0}`. The position decode is
strongly supported as:

```text
x = raw_x * sx + tx
y = raw_y * sy + ty
z = raw_z * sz + tz
```

Stored matrix values remain authoritative; exact runtime rounding still needs
comparison. Packed normals strongly support three 5-bit channels biased by 15,
with the high bit used as ADC, followed by normalization. Exact VU normal
handling remains unresolved.

ADC topology is verified for this subset. The first two vertices of every
packet have ADC set. A later set ADC bit suppresses the current triangle
without removing that vertex from strip parity, and each record's stored
triangle count equals its number of ADC-clear vertices. No separate index
array exists in the validated subset. Alternating strip parity is known, but
the native front-face convention is not, so Blender-ready winding is not yet
claimed. The experimental level importer compares each triangle normal with
its averaged decoded vertex normal and flips disagreeing triangles for
diagnostic display while preserving the correction count as metadata.

Coordinate lanes are signed 4.12 fixed point divided by 4096. V2-16 carries
UV0; in the validated V4-16 variant, XY is UV0 and ZW is an independent UV1.
The serialized selector is routed unchanged into the renderer: 0/1 select
UV0/UV1, 2/3 select UV2/UV3, 4-7 use the direct UV implementation, 8 selects
generated environment coordinates, 9/10 select generated reflection
coordinates, and 11 carries a serialized 4x4 matrix. Large UV0 ranges paired
with captured repeat/repeat state prove intentional tiling.

Direct STQ does not negate V. With decoded row zero represented at the top,
source V points down, so Blender flips decoded image rows once and does not
also flip UVs. Standard normalized host sampling requires no extra half-texel
bias. The optional six-float transform packing is preserved, but its
multiplication convention remains unresolved.

V4-8 is unsigned vertex diffuse RGBA rather than unconditional final prelight.
The `LIGHT_CUBIC_DIFFUSE` VU path builds a light term from the squared normal
components and six signed-axis coefficients, then computes:

```text
RGBout = trunc(cubic_light(normal) * vertex_rgb)
```

Source alpha remains continuation-dependent. The captured candidate family
has source alpha 127 but emits constant alpha 64 without reading that lane.
Preserve all four bytes even when a particular material continuation ignores
alpha. RenderZone fog is not established as the RGB source; the scene path
that chooses LightZones and populates the six cubic-light coefficients remains
open.

The standalone scanner preserves and reports the bounded packet bytes. The
experimental level importer creates Blender meshes from the same decoder and
retains packet hashes, source IDs, packet shapes, ADC, material IDs, UV layers,
and optional V4-8 vertex data as mesh data or custom properties.

#### Embedded static resources

The static custom block is relative to each serialized object and starts at
object offset `+0x40`. The generic object directory therefore discovers static
resources even when they are not the first serialized type and bounds their
chunks without scanning a CoreStream for signatures.

As a focused cross-check, `level.11.sdplatform_templar` contains 40 embedded
regular static objects with 357 records, 2,251 packets, 114,912 strip vertices,
and 62,014 emitted triangles. Every record resolves to a Material, and the
same packet bounds, position extrema, attributes, ADC topology, and triangle
count invariants used by the seven leading resources hold. The level importer
resolves Multi/LOD wrappers, selects LOD child zero, transposes source
row-vector instance matrices for Blender, and imports otherwise unreferenced
graph roots at identity.

#### Runtime material and GS-state evidence

Serialized and runtime material layouts are distinct. Serialized layers begin
at `+0x18` and contain a fixed `0x40` record, followed by a `0x40` matrix when
coordinate selector 11 is used and a six-float `0x18` transform when flag
`0x20` is set. Flag `0x40` selects a default transform without serialized
payload bytes. Runtime `Material::Layer` embeds a `HwLayer`; its
`HwLayer+0x58` field is a 64-byte transform object, not a second texture
reference.

One repeated SDDocking family has an exact three-pass capture:

| Pass | Serialized layer | Runtime texture | Blend equation | Established role |
|---:|---:|---|---|---|
| 0 | 0 | PSMT8 256x128, TBW 4 | `(Cs - 0) * FIX + 0`, FIX 128 | base/replacement |
| 1 | 1 | PSMT8 32x32, TBW 2 | `(Cs - 0) * Ad + Cd` | view-dependent detail/environment |
| 2 | 2 | PSMT4 256x128, TBW 4 | `(Cd - 0) * As + 0` | scalar alpha-mask modulation |

All three passes use modulate RGBA, PSMCT32 CLUTs in CSM1 with CSA 0 and CLD
1, repeat/repeat addressing, linear texel filtering, no active mip levels, no
alpha test, and `GEQUAL` depth testing. Persistent `TEXA` is zero. Serialized
coordinate selector bytes `{0, 10, 1}` route to
`{UV0, reflection, UV1}` for the three respective roles.

Attribution remains deliberately bounded. Within the captured SDDocking Core,
the family is structurally consistent with material `0x061763EA`, but material
`0x060E9986` has the same first two textures and packet group counts. Only
their third serialized texture differs, and the captured PSMT4 upload matches
neither one. Report the runtime family as a candidate for one of those two
materials, not as a proven capture of either. Object IDs are Core-local, so a
matching numeric ID in another Core does not inherit this attribution.

The four Street2 targets are now proven serialized `INDEX_4` textures sampled
as PSMT4 and selected by instanced geometry:

```text
0x01F70928
0x01F49D6C
0x01F49D86
0x00570004
```

None appears in the bounded 154-upload capture, its 15 target-sized payloads,
or its 19 candidate CLUT uploads. Independent corpus validation now proves
the serialized plane mapping, low-nibble-first PSMT4 decoding, row-major native
8x8 mips, and both complementary-alpha palette banks. All seven selected
Street2 references use `ALPHAADD`, so they select the normal palette bank.
Material `0x01F450D2` is one of those seven references and uses texture
`0x01F70928` through direct UV0. Its bounded Blender preview applies the
decoded texture and Native V4-8 color/alpha with the PS2 `128 == 1.0` scale,
then represents operation 5's source-over equation with a blended Principled
surface.

Street2 object `Instance 0118 0x0608B5EC` resolves from
`StaticMeshInstance` object `0x0608B58C` to one 585-triangle regular resource.
Its five records use the same single-layer direct-UV0 `ALPHAADD`, normal
palette, scalar-255, MODULATE, source-over state and all carry Native V4-8:

| Material | Texture | Format | Fade mode | Triangles |
|---|---|---|---:|---:|
| `0x060B062E` | `0x01F49CEA` | `INDEX_8` | 0 | 6 |
| `0x01F450D2` | `0x01F70928` | `INDEX_4` | 2 | 106 |
| `0x01F6C9FC` | `0x01F49D1E` | `INDEX_8` | 2 | 365 |
| `0x01F483DA` | `0x01F49D6C` | `INDEX_4` | 2 | 56 |
| `0x06083ACE` | `0x01F49D38` | `INDEX_8` | 2 | 52 |

The same bounded shader now covers all five records. `INDEX_8` admission is
limited to the matching serialized depth/alpha/TFX/framebuffer/VU state and
fade modes 0 or 2; unrelated operation-5 Materials are not enabled merely
because they use an indexed texture.

Street2 Material `0x01F7E8D4` is one documented `INDEX_8` member of that
bounded family. It is one Core-local Material definition reused by 11
instanced regular-static meshes, not two definitions despite a repeated
Blender display name. Its single layer resolves to 256x256 PSMT8 Texture
`0x01F70942`, direct UV0, the normal CSM1 palette, operation 5 `ALPHAADD`,
lighting mode 1, disabled Z-write, `GEQUAL` depth, alpha test `ALWAYS`,
repeat/repeat addressing, filter policy 4, scalar 255, and fade mode 2 with
values `(15, 0.5, 0.5, 20)`.

The decoded image is a graffiti/poster atlas with 24,157 zero-alpha, 22,268
partial-alpha, and 19,111 full-alpha texels across 110 distinct alpha values.
Its 11 users contribute 404 triangle occurrences. Every target mesh carries
Native V4-8; target-face RGB spans 0..128 and alpha is 121 or 128. The bounded
node graph therefore applies:

```text
decoded INDEX_8 RGBA * Native V4-8 RGBA * (255 / 128)
```

and sends the resulting alpha through a Blender `BLENDED` Principled surface
to approximate the proven source-over GS equation. Material custom properties
record `single-layer-index8-alphaadd-v4-8`, `INDEX_8`, and the PS2 color-one
value 128. The small emission boost remains diagnostic because retail
lighting mode 1 is not reconstructed.

These meshes are serialized static graffiti/poster overlays. They are not
runtime `DecalResource` projections and remain part of the level's static
resource graph independently of runtime decal support.

### Native skinned-mesh and Skeleton evidence

The serialized `RegularSkinnedMeshResource` stores a nullable Skeleton object
ID at `+0x24`; its PS2 custom block starts at `+0x40`. Each 16-byte mesh record
contains a stream-directory offset and flags, packet count, emitted triangle
count, and Material object ID. Declared chunks exactly fill the stream range
owned by the enclosing object-directory entry.

The independently reproduced retail corpus contains:

| Measurement | Value |
|---|---:|
| Regular skinned objects | 1,244 |
| Mesh records | 2,386 |
| DMA/VIF packets | 29,680 |
| Transform records | 11,322 |
| Packet transform groups / palette bytes | 52,249 |
| Logical position/normal pairs | 1,374,527 |
| Output strip vertices | 1,661,564 |
| Emitted triangles | 1,029,286 |
| Skeletons / source-order bones | 416 / 18,037 |

Each record directory stores packet keys, chunk-relative offsets, chunk
indices, and two still-unknown metadata arrays. Every packet index and offset
selects a bounded, 16-byte-aligned DMA `RET` packet in the declared object
chunk. Zero-packet records are retained as empty, non-renderable records
without dereferencing their nested directory offsets. Packets share the static
callable DMA prefix, then use:

1. a masked unsigned S8 stream of transform-group sizes;
2. signed V3-8 vectors;
3. masked unsigned S16 control words targeting the same VU address;
4. zero or more signed V2-16 or V4-16 tail attributes;
5. optional unsigned V4-8 vertex diffuse/color data with
   continuation-dependent treatment.

For every packet, if `G` is the group count and `M` is the number of V3/S16
entries:

```text
M = 2 * sum(group_size[0:G])
raw_control = control & 0xFFFF
target = (raw_control & 0x7FFF) / 3
adc = (raw_control & 0x8000) != 0
```

Each packet group consumes one palette byte that selects a 32-byte transform
record. That record's selector is the zero-based source-order bone index in the
resolved Skeleton. All 11,322 selectors are bounded by the packed Skeleton's
bone count, including the 26 records belonging to meshes whose direct Skeleton
token is null and whose wrapper supplies the Skeleton. Runtime EE code indexes
the live matrix array directly as:

```text
selected = matrix_base + 0x40 * transform_record.selector
```

There is no mesh-local remap or indirection. Each logical position/normal pair
uses exactly one selected matrix with implicit weight `1.0`; repeated output
targets implement strip or attribute-seam duplication rather than weighted
skin accumulation. With quantization-box center `A`, full extent `B`, signed
position bytes `p`, and selected affine columns `C0..C3`, the exact position
decode is:

```text
local = A + B * ((float(p) + 0.5) / 255.0)
position = C0 * local.x + C1 * local.y + C2 * local.z + C3
```

The normal path applies the selected matrix's three linear columns to signed
normal bytes divided by 127, then removes one uniform scale measured from
`C0`. Interpreter replay matches the documented position and normal equations.
The control low bits are always divisible by three, coalesced targets cover
every output vertex exactly once or twice, and each record's triangle count
equals its unique ADC-clear target count.

Packed Skeleton parent indices establish one indexed hierarchy per Skeleton:
bone zero is the sole root, each other parent precedes its child, and every
chain reaches the root. Animation-local matrices arrive in category-major
order, are scattered back to source-bone order, and are accumulated as:

```text
G[i] = L[i]                         if parent[i] == 0xFF
G[i] = G[parent[i]] * L[i]          otherwise

selected = G[transform_record.selector]
clip = K * Object * selected * decoded_local_position
```

The completed `G` array is copied directly to the allocation used by the draw
path. The serialized temporary 48-byte transforms are interned into a separate
global pool; the active selector path does not read that pool or apply a
post-hierarchy inverse-bind multiply. Those matrices strongly resemble
row-vector global inverse-bind transforms, but their semantic role remains
unverified.

The packet tails contain zero through four signed coordinate pairs. The first
pair is verified fixed-point texture data divided by 4096; later pairs still
need material-layer names. Optional V4-8 RGB reaches vertex color in some
material continuations, while RGB/alpha behavior remains material-dependent.

#### Standalone skinned-model import

The Blender add-on discovers uniquely selectable static and skinned graph
roots during the cached asset scan. In **KZ Tools > Import Model**, a skinned
root imports its selected LOD0 leaves, source-order Skeleton, rigid vertex
groups, native Materials, and packed images. Cache schema 2 records each
bank's lightweight clip identity and duration, allowing a multi-select clip
list without decoding payloads during UI changes. Model-Core clips and one
optional shared bank are selected by default, with **All** and **None**
controls.

A clip is admitted only when its binding covers every source Skeleton bone.
Extra unmatched bank tracks are preserved in Action metadata. The optimized
writer constructs layered FCurves directly with vectorized coordinates and
linear interpolation, reducing exactly constant channels to one key. The
original per-bone `keyframe_insert` writer remains visible and is the automatic
fallback for optimized Blender-writing failures. Decode and decomposition
failures do not fall back. The visible decomposition tolerance defaults to
`0.05`.

Standalone static roots use the same Multi expansion, LOD0 selection, packet
decoder, material construction, and packed-image path without creating an
Armature.

[tools/kz_skinned_preview.py](../tools/kz_skinned_preview.py) imports one
selected skinned graph from an authorized Core/CoreStream pair. It expands all
Multi children, selects LOD child zero, builds the packed Skeleton in
source-bone order, converts every target to one normalized vertex-group
assignment, attaches Armature modifiers, creates native Material nodes, packs
the selected decoded textures, performs a controlled deformation check, and
saves a reusable `.blend`.

When a Core has multiple appearance roots, pass the desired object token
explicitly:

```powershell
& 'D:\Blender Foundation\Blender 5.2\blender.exe' `
  --background --factory-startup `
  --python '.\tools\kz_skinned_preview.py' -- `
  'D:\private\Killzone.iso' `
  'localcache.ps2/lumps/entity.helghast.3rd.soldier.core' `
  --root-token 0x0053D442 `
  --blend '.\probe-output\helghast-soldier-rigged-textured.blend' `
  --png '.\probe-output\helghast-soldier-rigged-textured.png' `
  --report '.\probe-output\helghast-soldier-rigged-textured.json'
```

The report rejects unweighted vertices, multiple assignments, non-unit
weights, out-of-range selectors, triangle-count mismatches, unpacked selected
images, or a controlled bone rotation that fails to move its assigned
vertices independently. Material previews remain bounded diagnostics.
Blend shapes, `LinkedStaticMeshResource`, and placement of level
`SimpleAnimatingSkinnedMeshInstance` records are not yet connected.

To bind a clip from the same Core or a shared animation bank to the selected
model Skeleton, add an animation path and an unambiguous clip selector:

```powershell
& 'D:\Blender Foundation\Blender 5.2\blender.exe' `
  --background --factory-startup `
  --python '.\tools\kz_skinned_preview.py' -- `
  'D:\private\Killzone.iso' `
  'localcache.ps2/lumps/entity.example.core' `
  --root-token 0x01234567 `
  --animation-path 'localcache.ps2/lumps/animation.example.core' `
  --animation-token 0x07654321 `
  --blend '.\probe-output\example-animated.blend' `
  --report '.\probe-output\example-animated.json'
```

Use repeated `--animation-token` arguments to bake multiple uniquely keyed
tokens. If a bank legally repeats a token, select its directory identity with
`--animation-key 0xSECTION_OFFSET:OBJECT_INDEX`. A one-clip bank needs no
selector. The default policy keeps sampled track motion only; opt into the
separately evidenced Total Distance reconstruction with
`--reconstruct-root-trajectory`.

The Action baker rejects pose matrices whose shear cannot be represented by
Blender location/quaternion/scale channels. Its strict default decomposition
residual is `1e-5`. For a retail clip with intentional affine deformation,
pass an explicit approximation limit such as
`--animation-residual-tolerance 0.05`; the measured maximum remains recorded
in both the JSON report and the Action's
`killzone_decomposition_residual_max` custom property.

#### Packed animation tracks

Packed `SkeletonAnimationResource` data begins with a track count and packed
byte size, followed by sequential two-byte track headers and zero alignment
padding. The pure parser reproduces the bounded track walk for both 16-byte
inline and 64-byte streamed forms. It supports short and extended sample
counts and enforces the serialized payload order:

Retail inline clips retain a zero-length CoreStream directory range whose
nonzero offset is the current 64-byte-aligned stream cursor. Storage selection
therefore follows the generic flags and range size; an empty aligned anchor is
valid and does not make the inline clip streamed.

```text
time -> rotation -> translation -> scale
```

All nine time modes are decoded, including normalized 16-bit keys, explicit
24 Hz byte keys, implicit 6/12/24 Hz keys, and least-significant-bit-first
variable deltas at the retail 12/24/6 Hz rates. Exact translation support
includes mantissa-truncated float vectors, fixed 10:10:10 vectors, big-endian
sparse signed 14-bit vectors, and both adaptive metadata forms with per-axis
dequantization. Constant and keyed mantissa-truncated scale vectors are also
decoded and can be linearly sampled.

Rotation codecs 0 through 9 are decoded and sampled. This includes packed-u16
components, 10:10:10 reconstruction with its strict unit-sphere branch,
adaptive reconstructed-component quaternions with an optional W sign,
adaptive three-angle `qz*qy*qx` composition, and both fixed-axis encodings.
Fixed-axis keyed angles use the retail wrap logic, including its asymmetric
negative-delta branch.

Keyed codecs 2, 4, 7, and 8 use the documented VU0 `0x520` shortest-path
approximation. The implementation preserves its polynomial acos/sine/cosecant
paths and its unnormalized linear fallback for nearly identical endpoints;
it does not replace them with a host SLERP.

The 24-byte `SkeletonAnimationBoneMap` directory, inline/streamed clip
objects, authoritative duration, playback mode, event token, Compensate Root,
Total Distance, and BoneMap reference are preserved. Track names bind to the
first case-insensitive Skeleton-name match. Shared banks never select a
co-resident Skeleton implicitly; the caller supplies the model Skeleton.

The native evaluator resets category records, samples direct Q/QT/QTS locals,
uses static translation for category zero, applies category-two scale by
rotation column, and accumulates `parent_global * local`. The Blender writer
uses the serialized global inverse bind and actual edit-bone rest matrix,
rejects non-decomposable matrix bases above a declared residual tolerance,
maintains quaternion hemisphere continuity, and writes one `OBJECT`-slot
Action. Curves are baked at the requested rate, mapped through the scene frame
rate, and made linear; VU0 interpolation is exact at baked samples rather than
between sparse host keys. Repeat and held one-shot policy, clip/event identity,
root policy, basis, digests, and decoder status are retained as Action
metadata.

### Native Texture import

Nineteen variant-1 `Texture` resources use one identical, self-contained
layout: a 512x512 PSMT8 indexed image, eight 32 KiB image chunks, a CSM1
RGBA palette plus preserved auxiliary bytes, and a preserved 120-byte footer.
Standard PS2 PSMT8 unswizzling and CSM1 palette remapping were validated across
all 19 authorized-disc samples.

To import one in Blender:

1. In the add-on preferences, choose the authorized ISO or a directory
   containing `FILES.DAT` and `FILES01-04.DAT`.
2. Select **Scan / Rescan Importable Assets**.
3. Open **3D View > Sidebar > KZ Tools**.
4. Under **Import Texture**, select a cached entry and choose
   **Import Texture**.

The result is a persistent Blender Image with its archive path, source Core
SHA-256, decoded RGBA SHA-256, and encoding recorded as custom properties.
The original Core bytes and unknown regions remain authoritative. Edited-image
native export is deliberately unavailable until an encoder and game-side
round-trip are proven.

#### Embedded textures and level materials

A complete pass over all 653 catalogued Cores finds 111 variant-5 Cores that
declare an exact `Texture` type. Their 6,907 Texture sections contain 13,891
physical definitions, 13,718 logical Core-local tokens, and 305,231,104
payload bytes. The 47-level subset accounts for 12,613 physical definitions,
12,566 logical tokens, and 273,141,504 bytes. Only byte-equivalent duplicate
definitions are collapsed logically; every physical definition remains
represented for validation and future round-trip work.

The corrected embedded Texture header stores width, height, pixel format, mip
count, and chunk count at `+0x20` through `+0x34`; `+0x38` begins the chunk-size
array. The parser validates combined `0x100`-aligned mip storage, zero padding,
palette framing, every directory-owned CoreStream range, and all 22 inline
objects. Those inline objects comprise ten `INDEX_8`, six `ALPHA_8`, and six
`RGBA_8888` definitions; Street1 remains the sole level-inline case. The
matching ELF pixel-format descriptor table begins at VA `0x00519CD8`, file
offset `0x003DACD8`; file offset `0x003DAD00` is record zero's name pointer,
not the table base.

| Format | Physical | Logical | Blender policy |
|---|---:|---:|---|
| `INDEX_4` | 3,175 | 3,120 | exact PSMT4 image plus normal/complement palette variants |
| `INDEX_8` | 7,383 | 7,275 | exact PSMT8/CSM1 image |
| `ALPHA_4` | 496 | 496 | exact white-RGB normal/complement alpha ramps |
| `ALPHA_8` | 2,579 | 2,579 | exact white-RGB normal/complement alpha ramps |
| `GLOW_8` | 32 | 32 | exact index plane; dynamic artistic palette unresolved |
| `RGBA_8888` | 186 | 176 | packed RGBA base image |
| `RGB_888` | 36 | 36 | packed RGB base image with host-only opaque alpha |
| `INVALID_PIXELFORMAT` | 4 | 4 | explicit missing-texture node |

No other code in the 23-entry engine enum occurs anywhere in the complete
catalog. The add-on intentionally rejects those 15 descriptor-only formats
instead of guessing serialized layouts from names or channel masks.

After each Texture's palette word, the parser validates 36,752 serialized
span records as two identical halves. The 18,376 first-half runtime records
reconstruct every valid raw mip prefix byte-for-byte, including 2,209
multi-span textures and 3,084 serialized records with nonzero offsets.

Default-LOD geometry selects 11,870 Materials containing 30,994 variable-sized
layers: 30,814 Texture providers, 134 AnimatedTexture providers, and 46 null
providers. The parser consumes all 1,559 flag-`0x20` transforms without
creating any opaque/procedural providers. All 85 AnimatedTextures and their
1,302 ordered frames and positive durations are validated.

Blender receives native per-face material assignments and one node per
serialized layer. Normal and complementary decoded images are attached where
available. Proven selectors 0-7 are linked to direct native UV maps when no
unresolved optional transform applies; generated environment/reflection
routes, selector-11 matrices, and six-float transforms remain explicitly
unconnected. Normal imports connect only bounded single-layer, direct-UV
indexed `ALPHAADD` cases when every user mesh carries Native V4-8 color.
`INDEX_4` retains its target-validated gate; `INDEX_8` additionally requires
the exact matching serialized render state described above. Texture and
vertex RGBA are modulated at the PS2 `128 == 1.0` scale and operation 5 is
represented as source-over transparency. Other Texture layers remain
disconnected because full VU source color, lighting, layer composition, and
runtime mutation are not reconstructed. Generated preview scenes additionally
connect a bounded layer-0 `WRITE` diagnostic and save the 3D viewport in
Material Preview camera mode. A small emission boost keeps dark source
textures legible without claiming native lighting. Blue warnings identify
these previews as diagnostic rather than exact game material shading.

### Runtime decal evidence

The authorized NTSC-U catalog contains one shared
`localcache.ps2/lumps/custom.materialeffects.core` library with 11
`DecalResource` objects. No Core header contains a serialized
`DecalInstance`; level Cores also contain neither decal type name. The
instance, receiver groups, circular generated-vertex buffers, and individual
marks are created and maintained at runtime.

The parser preserves each resource's sort mode/order, per-receiver triangle
capacity and soft threshold, Random Rotation property, size range, fade-in
and fade time, clipping selection, Material token, identity words, and raw
serialized blob. The shipped resources all select BackToFront order 11 and
clipped projection, while capacities, thresholds, size ranges, fade times,
and Materials vary.

Pure runtime helpers reproduce the instruction-verified behavior that can be
used without guessing missing receiver data:

- one random scalar controls both size and angle, regardless of the reflected
  Random Rotation value on the traced retail path;
- unclipped marks use the oversized three-vertex
  `(-1,-1)/(3,-1)/(-1,3)` projector and `(0,0)/(2,0)/(0,2)` UVs;
- clipped footprints switch from that triangle to a square at `size == 0.2`,
  and the asymmetric back-side rejection accepts exactly `q == -0.5`;
- provisional budgets, approximately 1.5x capacity growth, the
  per-receiver `3*MaxDecals` cap, soft-pressure prefix selection, fade
  updates, RGB-preserving alpha replacement, wrapped ring spans, 48 queue
  buckets, signed depth keys, and descriptor bits are reproduced.

Projected geometry is deliberately not added to native level scenes because
the required live mark position/direction, random scalar, receiver/submesh
tag, receiver polygons, and lifecycle state are absent. Decal spans use the
ordinary multi-layer Material renderer; the add-on does not substitute a
hard-coded alpha-blend/no-Z-write preset. Street2 diagnostics now record the
11 shared resources and explicitly label runtime decal geometry as
unreconstructed.

Validate and inspect the shared resource policies without extracting them:

```powershell
python .\tools\kz_decal_probe.py 'D:\private\Killzone.iso' `
  --output '.\probe-output\decal-resources.json'
```

### Native level evidence

The catalog contains 47 paired `level.*.core` / `.corestream` resources: 39
campaign sublevels and 8 multiplayer levels. All 47 Core metadata files are in
`FILES03.DAT` and total 64,378,700 bytes; all 47 streams are in `FILES04.DAT`
and total 456,126,272 bytes. A further 41 self-contained variant-1
`*.missionscript.core` resources carry compiled or serialized mission scripts
separately from the spatial level lumps.

Level string tables identify zones, portals, render and light zones, static
and skinned instances, LODs, collision, paths, AI areas, lights, sounds,
effects, entities, and other gameplay objects. A native level is therefore a
compound Core object graph, not a geometry-only file.

`StaticMeshInstance` is not the owner or enumerator of all visible geometry.
Build a directed graph of regular, Multi, and LOD static resources using
wrapper-child references. Across the 47 level Cores:

| Measure | Count |
|---|---:|
| Regular static leaves | 9,327 |
| Multi-static wrappers | 137 |
| LOD-static wrappers | 2,793 |
| Resource roots | 5,896 |
| Roots referenced by static instances | 3,158 |
| Direct world roots with no instance reference | 2,738 |

Import every non-null instance reference by recursively expanding all Multi
children, selecting LOD child zero, and applying the instance matrix. Then
import each graph root not named by an instance using the same expansion at
identity. Direct regular leaves already contain level/world coordinates.
Do not independently import every regular leaf: that duplicates wrapper
children and alternate LODs. Two legal Mall and Docks1 instances reference
wrapper children rather than roots and must still be processed.

Default LOD0 expansion produces:

| Render path | Leaf occurrences | Packet occurrences | Triangle occurrences |
|---|---:|---:|---:|
| Direct world roots | 2,924 | 91,812 | 2,576,133 |
| Static instances | 22,109 | 91,913 | 2,326,919 |

These are draw occurrences rather than physical storage totals because
instances repeat resources and unselected LOD children remain stored.
Street2 specifically contains 72,276 direct-world and 60,477 instanced
triangles. An importer starting only from `StaticMeshInstance` can therefore
appear to import decals and props while dropping most of the level.

SDDocking resources `0x061005A8` and `0x0615026C` are direct world roots and
are unreachable from any `StaticMeshInstance`; their appearance in the GS
trace independently validates identity-placed direct-root rendering.

To import the implemented static render graph in Blender:

1. In the add-on preferences, choose an authorized ISO or a directory
   containing `FILES.DAT` and `FILES01-04.DAT`.
2. Select **Scan / Rescan Importable Assets**. The persistent cache orders
   campaign chapters numerically, labels them with their retail chapter name
   and human-readable area, then lists the eight retail multiplayer maps.
3. Open **3D View > Sidebar > KZ Tools**.
4. Under **Import Map**, select a map and choose **Import Map**.

**Import Map Textures** is enabled by default. Disabling it bypasses native
Texture, AnimatedTexture, and Material decoding and imports geometry with the
diagnostic fallback material.

The importer creates **Direct World (identity)** and **Props** collections.
**Props** contains **Static Instances** plus **Animated Skinned Instances
(metadata only)**. A `StaticMeshInstance` that selects one regular leaf is a
transformed mesh object keyed by its compact-directory identity. Null-resource
instances and occurrences that expand to multiple Multi children use an owner
Empty; selected leaves are identity-local children so the source placement
matrix is applied exactly once. Repeated occurrences remain distinct objects
while sharing one mesh datablock per exact regular-resource directory object.

`SimpleAnimatingSkinnedMeshInstance` records are preserved as owner Empties
because the level-instance graph is not yet connected to the standalone
skinned-model importer. Their resource, default-animation, blend-shape, and
optional proximity-parent references are retained without creating a Blender
parent relationship; runtime parent composition order is not yet proven.
Static and animated owners retain the Core-local name index and flag,
object/reference tokens, opaque footer, exact directory identity, and all 16
source matrix values plus their original `f32` bit pattern. Explicit
identity-matrix instances remain distinguishable from implicit direct-world
occurrences.

Each static mesh can contain `Native UV0`, `Native UV1`, `Native V4-8`,
`killzone_adc`, and `killzone_material_id` data when present in the source.
Native Materials are assigned per face, and exactly decoded selected textures
become persistent packed Blender Images referenced by inspection nodes with
proven direct UV links. Packing is required so decoded pixel buffers survive
saving and reopening a `.blend`.
When **Import Map Textures** is enabled, the regular importer connects bounded
single-layer direct-UV `WRITE` previews, exact indexed `ALPHAADD` cases with
matching state and Native V4-8 data, and clearly labeled decoded-color
diagnostics for structurally selected layers. Complex final shaders outside
those policies remain diagnostic and unconnected rather than inventing native
multi-pass composition.
For diagnostic `ALPHAADD` layers that fall outside exact GS-state admission
but have serialized Native V4-8 data, the visible preview applies the measured
`texture * V4-8 * (255 / 128)` color and alpha modulation while continuing to
label native depth, blend, and lighting behavior unresolved. This prevents a
uniform-white provider texture from hiding an authored V4-8 tint.
Source archive paths, Core/CoreStream hashes, packet counts, triangle counts,
graph totals, texture formats, and decoding status are stored as custom
properties.

For a reproducible headless preview plus a per-texture investigation log, run
[tools/kz_level_preview.py](../tools/kz_level_preview.py) from the workspace
root:

```powershell
& 'D:\Blender Foundation\Blender 5.2\blender.exe' `
  --background --factory-startup `
  --python '.\tools\kz_level_preview.py' -- `
  'D:\private\Killzone.iso' `
  'localcache.ps2/lumps/level.10.mountainpass.core' `
  --blend '.\probe-output\level10-mountainpass-complete-static-preview.blend' `
  --png '.\probe-output\level10-mountainpass-complete-static-preview.png' `
  --overview-png '.\probe-output\level10-mountainpass-complete-static-overview.png' `
  --material-png '.\probe-output\level10-mountainpass-material-diagnostic.png' `
  --report '.\probe-output\level10-mountainpass-complete-static-diagnostics.json' `
  --texture-log '.\probe-output\level10-mountainpass-texture-investigation.json'
```

`resolved-packed-image` means the serialized provider resolved, decoded, and
survived as a packed Blender Image. It does not claim that the complete native
material renders exactly. `diagnostic-write-preview` identifies purpose-built
layer-0 `WRITE` previews, `diagnostic-color-preview` identifies a structurally
selected decoded color layer, while
`inspection-only-final-shader-unconnected` identifies layers preserved for
inspection without reconstructed composition. The log separately groups
generated-coordinate routes, unresolved six-float transform convention,
runtime palette selection, animated first-frame previews, RGB888 host-only
alpha, and missing UV data.

The Mountainpass validation resolves all 299 selected textures with no missing
providers: 231 `ALPHA_8`, 47 `INDEX_8`, 18 `INDEX_4`, two `RGBA_8888`, and one
`RGB_888`. Its material diagnostic connects 276 of 277 selected Materials:
256 layer-0 `WRITE` previews and 20 decoded-color diagnostics. One unrelated
complex material remains unconnected rather than broadening admission beyond
the four documented structural families. Exact bounded V4-8 `ALPHAADD`
admission remains zero for this level.

Material `0x00FB0B4A` is the repeated foliage family. Both direct-UV0 layers
use 256x256 `INDEX_8` Texture `0x00561068`: layer 0 is `LIGHTMAPCOLOR` with
Z-write and alpha `GREATER 204`, and layer 1 is source-over `ALPHAADD` without
Z-write. The atlas has 46,349 zero-alpha and 19,187 partial-alpha texels. Its
two regular-static definitions appear in 411 instance occurrences and 6,576
triangle occurrences. The diagnostic connects the decoded color and alpha
once; it does not claim to reproduce the two native passes, their VU features,
or lighting.

Material `0x005436FE` is the direct-world sandbag/rockwork family. Its
single direct-UV0 `ALPHAADD` layer uses fully opaque 256x256 `INDEX_8` Texture
`0x005417C8`, lighting mode 1, Z-write, TFX policy 2, and no Native V4-8.
Fourteen selected direct occurrences contribute 10,725 triangle occurrences.
The decoded-color diagnostic is therefore intentionally separate from the
Street2 V4-8 source-over preview.

`Direct 0001 0x00566D60` has four material records and 2,266 triangles. The
diagnostic selects layer-0 decoded color from Materials `0x00564AF0`,
`0x0054701E`, `0x00547702`, and `0x00546F22`, using UV0, UV0, UV2, and UV0
respectively. The first layer's six-float transform convention remains
unresolved, so its graph records that the transform was ignored instead of
silently presenting the mapping as exact.

This is a static render-graph importer, not a complete semantic level importer.
Native material dependencies are assigned structurally, not rendered with
their final game equations. Collision, skinned actors, paths, AI, entities,
effects, lights, gameplay objects, higher LOD selection, and native export are
not imported.

## Bundle layout

```text
bundle/
  manifest.json
  meshes/
    example.kzm
  original/
    example.mss
```

Each mesh entry contains the `.kzm` path and digest, material metadata, and an
optional opaque source blob with its format label and digest. The manifest is
written last so an interrupted export is not presented as a complete bundle.

## Next evidence gate

The complete default-LOD static resource graph is now implemented. The next
native milestones are:

1. Reconstruct generated environment/reflection coordinates, optional
   transform multiplication, and full material composition without
   over-attributing the bounded three-pass runtime capture.
2. Prove native handedness, unit scale, and front-face convention, and identify
   the dynamic source that gives GLOW palettes their artistic meaning.
3. Trace the remaining static VU1 program and caller-provided value for address
   `0x1EE`, including LightZone selection and cubic-light coefficients.
4. Place level `SimpleAnimatingSkinnedMeshInstance` graphs, connect their
   explicit default-animation edges, and validate selected BYOD retail clips
   against runtime poses. Complex events, crossfades, partial-body masks, and
   additive/reference-removal ordering remain separate evidence gates.
5. Capture live decal marks and receiver polygons/tags before connecting the
   proven projection, clipping, lifecycle, and ordinary Material submission
   path to Blender geometry.
6. Add collision and other level semantics only after their resource
   relationships and coordinate transforms are independently validated.
7. Keep native export blocked until unchanged parse/serialize identity,
   edited-data encoding, archive repacking, and game-side acceptance are
   demonstrated.
