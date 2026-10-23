# Killzone PS2 material and shader replication

This document records the material evidence needed to improve the Blender
preview of *Killzone* for PlayStation 2. It applies to the NTSC-U executable
`SCUS_974.02` (XOR CRC `0xCAAEC49C`), Killzone Asset Tools `0.13.0`, and
Blender 5.2.x. It does not claim that another region or build has identical
data.

The importer at this research snapshot was deliberately structural and
diagnostic. The proposals below do not turn an inferred effect into a
native-format fact and do not imply that native material export is safe.

## Current implementation status

Killzone Asset Tools 0.13.1 now implements the low-risk subset established by
this research:

- exact decoding of the observed indexed, alpha-only, RGB, and RGBA texture
  encodings;
- host-corrected static and skinned direct UV layers, authored repeat-tile
  offsets, mixed-axis wrap, and bounded axis-aligned six-float scale/offset
  transforms;
- named shader profiles, explicit unimplemented-state metadata, unlit bounded
  output, and byte-aware native alpha-test comparisons;
- bounded `WRITE` and V4-8 `ALPHAADD` previews;
- the HQ Grounds `ALPHAADD + LAYERBUMPDOT3` scalar/light composition; and
- the diagnostic `MASKEDWRITE + ALPHAADD` foliage cutout.

Static maps and standalone static/skinned models share these material and UV
policies. Generated environment/reflection coordinates, rotated or sheared
six-float transforms, AnimatedTexture playback, dynamic GLOW palettes, and
general destination-dependent multipass composition remain unresolved. The
maintained summary is in
[Architecture: Material preview architecture](./architecture.md#11-material-preview-architecture);
the measured contracts remain in this document and the
[technical reference](./technical-reference.md#embedded-textures-and-level-materials).

## 1. Evidence policy and snapshot

The labels used here are:

- **Verified**: established by bounded retail-corpus parsing, matching
  executable control/data flow, a GS trace, or an independently tested GS
  implementation. A runtime-verified path is not automatically a
  corpus-observed serialized value.
- **Strong inference**: fits the corpus and known PS2 behavior, but still needs
  a targeted runtime comparison.
- **Unknown**: the original value must be preserved; a Blender representation
  may be diagnostic but must not be called native.
- **Host approximation**: a useful Blender rendering choice, not a game-format
  claim.

Research snapshot: **2026-10-23**.

| Source | Snapshot used | Role |
|---|---|---|
| `D:\Github\Killzone_Blender` | HEAD `3005cb6c262f27c1310191dfe4362d03879de9a4` plus the pre-existing local working tree | Parser, Blender adapter, tests, and local probe output |
| `D:\Github\KillzoneRecomp-Extended` | HEAD `437e86b55aeaed14b05142cf35758c0e44b3faab` plus the pre-existing local working tree | Executable analysis, GS traces, renderer integration, and format corpus |
| `D:\Github\KillzoneRecomp-Extended\ext\pcsx2` | `d3e6982a3e36647d642bcd805d3ca5b442d0975c` | Independent GS registers, local-memory layouts, CLUT behavior, and raster state |

The principal local evidence is:

- `D:\Github\KillzoneRecomp-Extended\docs\assetforms.md`, especially sections
  3.2-3.8, 4.5-4.14, 5.4, 6.7, and 10;
- `D:\Github\KillzoneRecomp-Extended\docs\findings.md`, especially the
  post-process, fog/draw-distance, shadow, and specular findings;
- `io_scene_killzone\core\native_level_material.py`,
  `io_scene_killzone\core\native_texture.py`,
  `io_scene_killzone\core\native_static_geometry.py`,
  `io_scene_killzone\core\native_skinned_geometry.py`, and
  `io_scene_killzone\native_level_io.py`;
- `probe-output\native-level-material-corpus-diagnostics.json` and the
  Street2/Mountain Pass diagnostics listed in section 12.

The corpus report covers 47 levels, 11,870 selected Materials, and 30,994
selected layers. “Selected” means reachable from the default-LOD direct world
or placed instances; it is not a count of every dormant LOD and Material in
the archives.

## 2. Conclusions that should drive implementation

| Finding | Confidence | Consequence for Blender |
|---|---|---|
| A Material is an ordered, variable-length list of layers. Optional transform payloads invalidate a fixed layer stride. | Verified | Preserve layer order and parse sequentially; never select one “main texture” by guessed offset. |
| Material and texture tokens are scoped to their owning Core. | Verified | Cache and resolve by Core plus object identity, not by the numeric token globally. |
| PSMT8/PSMT4 plane order, CSM1 palette order, direct RGBA/RGB packing, and mip framing are known for all observed formats. | Verified | Texture decode can be exact; shader uncertainty must not be blamed on swizzling. |
| Direct coordinate pairs are signed 4.12 values. Up to four pairs are retained, while selectors 8-10 generate coordinates from normals/matrices. | Verified | Named UV maps are correct for selectors 0-7 only; reflection/environment routes need separate graphs. |
| V4-8 RGB is a vertex diffuse/albedo input on the cubic-light path, not always final prelighting. Its alpha continuation varies. | Verified | Do not globally wire the attribute straight to final color or opacity. |
| Material operation numbers are engine enums that choose VU/color and GS-state policies; they are not raw `ALPHA` register values. | Verified | Reconstruct an ordered operation, not a generic Principled texture stack. |
| The common `ALPHAADD` policy is GS source-over, but GS alpha uses `128 == 1.0` and Eevee cannot reproduce all ordering/depth cases. | Verified / host-limited | A bounded source-over preview is valuable, but must remain labeled when state or sorting is missing. |
| There is no GS back-face-cull state. Killzone may cull before the GS, but the exact game convention is still unknown. | Verified / unknown | Leave Blender back-face culling off unless a mesh family is independently proved. |
| Conventional per-pixel normal/specular mapping is not evidenced in this path. Lighting arrives at the GS as VU vertex color, texture/lightmap passes, and generated coordinates. | Verified | Principled metallic/specular defaults are artistic, not native. |
| `GLOW_8`, Material operation `GLOW`, and the framebuffer-alpha post-process glow are related concepts but not proved to be a one-to-one chain. | Verified separation | Preserve glow indices/state; do not invent an emission strength. |
| RenderZone fog data is known and GS fog blending is known, but the exact world-distance-to-fog-factor function is not. | Partly verified | A Blender volume or depth mix is an approximation and belongs at scene level, not in every material by default. |

## 3. Material records, providers, and mesh bindings

### 3.1 Serialized Material grammar

`io_scene_killzone\core\native_level_material.py` defines
`NativeMaterialLayer` at line 115 and parses selected Materials in
`parse_selected_materials` at line 878. The stable serialized grammar is:

```text
0x00  u32  layer_count
+0x18       first layer

layer:
+0x00  u32  provider_token
+0x04  u8   flags
+0x05  u8[3] zero
+0x08  u32  0x74                         # runtime HwLayer framing size
+0x0C  u8   operation
+0x0D  u8   lighting_mode
+0x0E  u8   z_write
+0x0F  u8   framebuffer_selector
+0x10  u8   depth_comparison
+0x11  u8   alpha_test
+0x12  u8   alpha_reference
+0x13  u8   wrap_s
+0x14  u8   wrap_t
+0x15  u8   filter_policy
+0x16  u8   texture_enabled
+0x18  u8   tfx_policy
+0x19  u8   alpha_source_mode
+0x1A  u8   alpha_scalar
+0x1B  u8   fade_mode
+0x1C  f32[4] fade_values
+0x31  u8   coordinate_selector
+0x32  f32  direction_x
+0x36  f32  direction_y
+0x3A  f32  direction_z
+0x3E  u8   vu_feature_0
+0x3F  u8   vu_feature_1
```

After the fixed `0x40` bytes:

1. selector 11 appends a `0x40`-byte 4x4 matrix;
2. flag `0x20` appends six floats (`0x18` bytes);
3. flag `0x40` asks the runtime to create a default transform but consumes no
   serialized bytes.

The 47-level selected corpus contains 29,435 flag-`0x00` and 1,559
flag-`0x20` layers. It contains no flag-`0x40` layer and no selector-11
matrix, so those two paths are executable-verified but not exercised by this
serialized sample. The post-layer Material tail is 8 or 16 bytes; its first
word remains unnamed.

The corresponding runtime structures are different:

```text
Runtime Material               size 0x30, layer_count +0x1C, layer* +0x24
Runtime Material::Layer        size 0x90, provider +0x08, HwLayer +0x0C
Runtime HwLayer                size 0x74, texture* +0x18,
                               matrix object +0x58, 6f object +0x5C
```

Runtime offsets must not be reused as serialized offsets. In particular,
Material `+0x58` is not reliably “layer 1.”

### 3.2 Provider and identity rules

Every nonzero selected provider token resolves uniquely in the same Core to a
`Texture` or `AnimatedTexture`; zero denotes an explicit null provider:

| Provider kind | Selected layers |
|---|---:|
| Texture | 30,814 |
| AnimatedTexture | 134 |
| Null | 46 |
| Other/procedural | 0 |

Core-local scope matters. For example, texture token `0x00570004` is
`INDEX_4` in Street2 and `INDEX_8` in Park2. Duplicate directory definitions
inside one Core are byte-equivalent in the validated corpus, but physical
entries still need to be retained for round-trip provenance.

`AnimatedTexture` is a provider animation, not evidence of UV animation:

```text
+0x00  u32  frame_count
+0x20  frame[frame_count]

frame +0x00  u32  Texture token
      +0x04  f32  duration
```

There are 85 objects and 1,302 frames; every duration is finite and positive.
Frame counts from 2 through 41 occur. Duration units, loop policy, and phase
are not yet proved.

### 3.3 Static and skinned bindings

A static `RegularStaticMeshResource` record is 24 bytes. Record `+0x00` is
the Core-local Material token, `+0x04` is emitted triangle count, `+0x08` is
packet count, and attribute flag bit `0x10` at `+0x0B` tracks the optional
V4-8 stream. This is the per-face Material binding used by
`native_level_io.py:_build_resource_mesh`.

A skinned record is 16 bytes:

```text
+0x00  u32  high 16 bits directory offset, low 16 bits flags
+0x04  u32  packet count
+0x08  u32  emitted triangle count
+0x0C  u32  Core-local Material token
```

Both record families have corpus-wide type-checked Material resolution.
Layer order is ascending serialized order. A runtime layer mask may suppress
layers, but no evidence shows it reordering the surviving passes.

## 4. Textures, CLUTs, swizzles, and color interpretation

### 4.1 Variant-5 Texture framing

The prefix parsed by `native_level_material.py:parse_level_textures` is:

```text
+0x00  u32  no_streaming
+0x04  u32  linear_storage
+0x20  u32  width
+0x24  u32  height
+0x28  u32  engine pixel-format enum
+0x2C  u32  zero in the validated corpus
+0x30  u32  mip_count
+0x34  u32  chunk_count
+0x38  u32  chunk_size[chunk_count]
```

For a streamed object the footer then contains palette chunk index, a zero
word, count, and duplicated `<u8 chunk, u8 zero, u16 byte_offset, u16 QWC>`
span records. The runtime consumes one half. Concatenating
`chunk[offset:offset + QWC*16]` reconstructs every raw mip prefix in the
corpus.

Mips are contiguous:

```text
raw_bytes = sum(ceil(max(1,W>>m) * max(1,H>>m) * bpp / 8))
pixel_storage_bytes = align_up(raw_bytes, 0x100)
```

There is no per-mip alignment. Chunks and spans are transport partitions, not
mip boundaries. Indexed palette data follows the one final `0x100`
alignment. The material state can emit `MIPTBP1` for three mip levels; no
`MIPTBP2` write was found in this path.

### 4.2 Observed level formats

| Code | Format | Level objects | Sampling PSM | Decoding status |
|---:|---|---:|---|---|
| 0 | `INDEX_4` | 3,034 | PSMT4 | Exact plane and 16-entry CT32 CSM1 CLUT |
| 1 | `INDEX_8` | 6,320 | PSMT8 | Exact plane and 256-entry CT32 CSM1 CLUT |
| 2 | `ALPHA_4` | 494 | PSMT4 | Exact plane; generated white RGB, alpha `17*n` |
| 3 | `ALPHA_8` | 2,564 | PSMT8 | Exact plane; generated white RGB, alpha `n` |
| 4 | `GLOW_8` | 31 | PSMT8 | Exact indices; dynamic palette producer unresolved |
| 5 | `RGBA_8888` | 134 | PSMCT32 | Tight little-endian R, G, B, A |
| 13 | `RGB_888` | 32 | PSMCT24 | Tight little-endian R, G, B; file has no alpha |
| 22 | invalid sentinel | 4 | none | Explicit empty/missing placeholder |

The remaining engine enum descriptors are declared by the ELF but have no
serialized retail samples in the complete variant-5 scan. They must not be
advertised as supported layouts merely because a runtime table contains a
PSM value.

For `RGB_888`, the importer's opaque alpha is a host convenience. Native
PSMCT24 sampling obtains alpha from persistent `TEXA`; the material-state
packet does not write `TEXA`. Exact replay therefore requires a captured or
otherwise proved persistent `TEXA` value.

### 4.3 Exact PSMT8 and PSMT4 plane order

For mip width `W >= 16`, the serialized PSMT8 byte address is:

```text
block = (y & ~15)*W + (x & ~15)*2
swap  = (((y + 2) >> 2) & 1)*4
py    = (((y & ~3) >> 1) + (y & 1)) & 7

offset = block + py*(2*W) + ((x + swap) & 7)*4
       + ((y >> 1) & 1) + ((x >> 2) & 2)
```

For PSMT4:

```text
block = (y & ~15)*W + (x & ~31)*2
swap  = (((y + 2) >> 2) & 1)*4
py    = (((y & ~3) >> 1) + (y & 1)) & 7
u     = ((x + swap) & 7) + ((x >> 1) & 8)
k     = ((y >> 1) & 1) + ((x >> 2) & 2)

nibble = block + py*(2*W) + 4*u + k
offset = nibble >> 1
```

An even nibble index selects the low nibble. Widths below 16 are row-major
for both formats and PSMT4 remains low-nibble-first. These functions are
implemented at `native_texture.py:_psmt8_offset`,
`unswizzle_psmt8`, and `unswizzle_psmt4`.

The game uploads the same bytes without a CPU transform. At widths at least
16 it aliases PSMT8 data through PSMCT32 at half width/height and PSMT4
through PSMCT16 at half width/height. The composed upload and GS sample
addresses prove the serialized mappings; “Morton order” is not an adequate
description.

### 4.4 CSM1 palettes and alpha

All five indexed/plane paths use PSMCT32 CLUT storage, CSM1, CSA 0, and CLD 1.
For a 256-entry palette:

```text
P(i) = (i & 0xE7) | ((i & 0x08) << 1) | ((i & 0x10) >> 1)
```

This swaps entries 8-15 with 16-23 in each group of 32. It applies to palette
order, not to pixel order, and must not be applied twice.

`INDEX_4` has a `0x100`-byte two-bank palette object. Both 16-entry banks have
identical RGB and complementary alpha. `INDEX_8` has a `0x500`-byte object:
a header, 256 RGBA entries, and a zero tail. ALPHA palettes are generated as
white RGB with normal/complement alpha. A derived runtime gate chooses the
complement only for `ALPHASUB` or `LAYERBUMPEMBOSSINV` under additional
alpha-scalar/fade/VU conditions.

CLUT alpha is a full stored byte. Do **not** apply the framebuffer-read alpha
doubling sometimes used for visualization: PSMCT32 CLUT alpha survives TEXA.
At the GS arithmetic level `0x80` is the conventional value for 1.0, so
texture/vertex modulation and blend-factor interpretation are separate from
file-byte preservation.

`GLOW_8` has a proved index plane and a proved palette transform: logical
0-127 are zero and logical 128-255 are built from a reversed dynamic
`T[0x300]` color table with alpha 128-255, then permuted through `P(i)`.
The producer and artistic meaning of `T` are unknown. Until captured, a
missing/dynamic-palette marker is more faithful than fabricated RGB.

### 4.5 Orientation, texel centers, and color space

Direct GS STQ does not negate V. Decoded row zero is the image top; Blender's
image and UV conventions require one deliberate source-to-host orientation
conversion. The current pair is internally consistent:

- `_create_level_image` reverses rows while filling the Blender image;
- `native_uv_to_blender` returns `(u, 1-v)`.

A normalized Blender sampler already addresses texel centers correctly. Do
not add a half-texel bias.

There is no native sRGB flag or linear-light shader stage in this evidence.
The GS performs fixed-point channel arithmetic. Therefore:

- **raw-reference mode** should treat channels as numeric data, do operation
  math before any artistic color transform, use an Emission output, and use a
  Raw/controlled view transform for pixel comparison;
- **artist mode** may mark visible color atlases as sRGB and masks as
  Non-Color before feeding Principled BSDF, but that is a host
  interpretation;
- lightmaps, alpha planes, scalar masks, and V4-8 arithmetic must not receive
  an accidental sRGB conversion.

The current importer marks `ALPHA_*` images Non-Color and other decoded
images sRGB. That is sensible for an art preview but is not a byte-exact GS
mode.

## 5. UV channels, generated coordinates, and animation

### 5.1 Geometry streams

Static and skinned V2-16/V4-16 coordinate pairs are signed 4.12 fixed point:

```text
u = signed16_x / 4096
v = signed16_y / 4096
```

For V4-16, XY is UV0 and ZW is UV1. Later coordinate commands can provide
UV2 and UV3. The importer correctly retains up to four layers as
`Native UV0` through `Native UV3`. Terminal V1-8/V2-8/V3-8 streams are a
different command class and must not be exposed as proved UVs.

The source STQ path multiplies U and V by per-vertex Q and the GS divides by
Q. Linear filtering chooses neighbors around `floor(U-0.5)` and
`floor(V-0.5)`. Tiled coordinates well outside 0-1 are common and verified.

### 5.2 Coordinate selector

Serialized layer byte `+0x31` reaches the renderer's selector dispatch
without reinterpretation:

| Selector | Verified runtime route | Selected-layer count | Blender policy |
|---:|---|---:|---|
| 0 | UV0 / STQ0 | 14,780 | `UV Map("Native UV0")` |
| 1 | UV1 / STQ1 | 9,188 | `UV Map("Native UV1")` |
| 2 | STQ2 | 2,307 | `UV Map("Native UV2")` |
| 3 | STQ3 | 299 | `UV Map("Native UV3")` |
| 4-7 | same implementation as STQ0 | 0 | UV0, while retaining selector value |
| 8 | generated environment | 299 | diagnostic normal/environment graph only |
| 9 | generated reflection | 87 | diagnostic reflection graph only |
| 10 | generated reflection | 4,022 | diagnostic reflection graph only |
| 11 | custom 4x4 matrix | 0 | preserve; serializer path unexercised |
| 12 | angular 1D | 12 | preserve/unconnected until equation is traced |
| 13 | directional 1D | 0 | preserve/unconnected |
| 14 | projection | 0 | preserve/unconnected |
| 15 | projection with alpha back-test | 0 | preserve/unconnected |
| 16-21 | distinct unnamed variants | 0 | preserve/unconnected |

Generated environment/reflection routes use normals and matrices, not stored
UV lanes. A Blender Texture Coordinate **Reflection** or Geometry **Normal**
route is a useful diagnostic starting point, but the projection and matrix
conventions must be proved before it is labeled native.

Flag `0x20` packs its six floats into two runtime rows:

```text
[p0, p1, 0, 0]
[p2, p3, p4, p5]
```

The packing is exact. The light-hovertank spotlight fixture proves the bounded
axis-aligned subset when the cross terms are effectively zero:

```text
u' = p0*u + p4
v' = p3*v + p5
```

The importer applies that scale/offset in source UV space and then performs
the host V conversion. Rotated or sheared records, such as Mountain Pass
Material `0x00564AF0`, remain preserved and unconnected because their complete
multiplication convention is still unresolved.

### 5.3 Texture animation

The current material graph stores all frame tokens/durations as JSON but
shows only frame zero. A future implementation should not animate the
`Image Texture.image` pointer by assumption. Safe choices are:

1. build a real Blender image sequence only after duration units and looping
   are proved; or
2. keep one Image Texture per native frame and drive a discrete switch node,
   retaining every unequal duration.

Do not infer scrolling, rotation, or UV animation from `AnimatedTexture`.
Those objects select texture frames; coordinate animation remains unproved.

## 6. Vertex color and lighting

### 6.1 V4-8 semantics

Static opcode `0x6E` and the equivalent skinned stream provide unsigned RGBA
bytes. Attribute bit `0x10` in the static mesh record predicts the stream.
The importer stores it as POINT-domain `BYTE_COLOR` named `Native V4-8`;
vertices without a stream receive diagnostic `(128,128,128,128)`.

The verified cubic-diffuse VU path uses squared normal components and six
signed-axis light coefficients, then computes:

```text
RGBout = trunc(cubic_light(normal) * V4_8.rgb)
```

PS2 color one is 128. For a texture MODULATE path the normalized Blender
equivalent of the byte arithmetic begins with:

```text
rgb = texture_rgb * vertex_rgb * (255 / 128)
```

The bounded importer preview implements this scale in
`_connect_v4_8_modulation`. That does not prove final lighting: the cubic
term is still absent. V4-8 alpha is continuation-dependent. One path ignores
it and emits constant alpha 64; another can use half of the vertex byte.
Never wire V4-8 alpha globally without proving the active VU continuation.

### 6.2 Prelight, dynamic light, and zones

`RegularStaticMeshResource` declares reflected properties for
`Lighting.Prelit`, receiving/casting dynamic shadows, casting static shadows,
and lightmap density. The level import currently does not expose their
values as a lighting controller.

The level corpus has 205 LightZones, 2,061 shape records, and 478 light
references: 284 resolve to `DirectionalLight`, 194 to `OmniLight`. The shape
basis is orthonormal and strongly resembles an oriented box, but the exact
shape convention and, more importantly, the scene path that selects a
LightZone and fills the six VU coefficients remain unknown.

Practical Blender policy:

- for a proved prelit path, use V4-8-derived color through Emission to avoid
  lighting it twice;
- for a proved cubic-light path, either implement the six-coefficient node
  equation after coefficient provenance is available or use Principled
  diffuse plus imported lights as an explicitly artistic approximation;
- when V4-8 is absent, do not infer “unlit,” white vertex color, or a fixed
  material color. Lighting mode 1 occurs both with and without V4-8.

### 6.3 Specular and normal-map claims

The captured GS path receives finished vertex color, texture coordinates, and
material passes. It has no per-pixel source normal/light-vector stream from
which to reconstruct a conventional modern specular term. Imported source
normals exist because VU lighting and generated coordinates need them, not
because the GS executes a Principled-like BRDF.

Operations `LAYERBUMPEMBOSS*`/`LAYERBUMPDOT3` and HIGHLIGHT requests exist,
but operation names alone do not establish a conventional normal-map role.
One exact HQ Grounds packet family is now bounded as a scalar framebuffer
multiplier. Its Native V4-8 alpha is uniformly 127; the color-stream
alpha-divide-by-two continuation truncates this to 63. `ALPHA_8` texture
alpha plus 63 is saturated by HIGHLIGHT, then operation 18 policy `0x89`
scales destination RGB by that alpha over native color-one 128. Other
operation-18 families remain continuation- and runtime-dependent, and at
least one retained capture is affected by runtime mutation or attribution
collision. The very common operation 18 is never sufficient evidence to
connect a texture to Blender **Normal**.

For native-reference mode, use Emission or zero out unproved Principled
metallic/specular contributions. For artist mode, Principled roughness,
specular, normal maps, and environment lights may be offered as enhancements,
but they must be opt-in and named as such.

## 7. Layer operations and GS state

### 7.1 Operation table

The operation byte indexes engine policy tables. The GS alpha register uses
`(A-B)*C+D`; the low-byte policies observed in the executable are:

| Value | Name | Selected count | GS alpha policy / side effect |
|---:|---|---:|---|
| 0 | `WRITE` | 9,530 | `B ? 0x44 : 0xA8` |
| 1 | `MUL2X` | 0 | `0x89` |
| 2 | `ALPHABLEND` | 0 | `0x89` |
| 3 | `ALPHAMUL` | 192 | `B ? 0x48 : 0x68` |
| 4 | `ALPHAMUL2X` | 4 | `B ? 0x42 : 0x62` |
| 5 | `ALPHAADD` | 7,416 | `0x44` |
| 6 | `ALPHASUB` | 96 | `B ? 0x46 : 0x89`; may select complement palette |
| 7 | `WRITEMASK` | 222 | `0x89` |
| 8 | `MASKEDWRITE` | 286 | `0x48` |
| 9 | `MASKEDADD` | 2 | `0x42` |
| 10 | `MASKEDSUB` | 0 | `0x46` |
| 11 | `LIGHTMAPCOLOR` | 616 | `0x6A` |
| 12 | `LIGHTMAPCOLOR2X` | 959 | `0x54` |
| 13 | `LIGHTMAPINTENSITY` | 3,406 | `0x58` |
| 14 | `LIGHTMAPINTENSITY2X` | 20 | `0x52` |
| 15 | `GLOW` | 0 | `0x89` |
| 16 | `LAYERBUMPEMBOSS` | 0 | `0x89` |
| 17 | `LAYERBUMPEMBOSSINV` | 0 | operation-6 policy; requests HIGHLIGHT |
| 18 | `LAYERBUMPDOT3` | 8,214 | `0x89`; requests HIGHLIGHT |
| 19 | `PROJECTIVESHADOW` | 31 | `0xA8` |
| 20 | `STENCILSHADOW` | 0 | operation-0 policy |

Zero means absent from the selected 47-level layer set, not impossible at
runtime. `B` is a runtime policy result derived from alpha/fade state; it is
not another serialized byte.

The corresponding RGB equations are:

```text
0xA8  (Cs - 0 ) * FIX + 0
0x44  (Cs - Cd) * As  + Cd       source-over form
0x89  (Cd - 0 ) * As  + 0
0x68  (Cs - 0 ) * FIX + Cd
0x48  (Cs - 0 ) * As  + Cd
0x62  (0  - Cs) * FIX + Cd
0x42  (0  - Cs) * As  + Cd
0x46  (0  - Cd) * As  + Cd
0x6A  (0  - 0 ) * FIX + Cd
0x54  (Cs - Cd) * Ad  + Cd
0x58  (Cs - 0 ) * Ad  + Cd
0x52  (0  - Cs) * Ad  + Cd
```

`Cs/Cd` are source/destination colors, `As/Ad` source/destination alpha, and
`FIX` is the fixed GS blend factor. The RGB equation alone is not the whole
operation: framebuffer masks, alpha writes, alpha-fail behavior, Z state,
PABE/FBA, TCC/TFX, and VU source-color construction can still matter. For
example, `0x6A` leaves destination RGB unchanged but may serve another pass
role. An operation name must not be translated to a Blender node solely from
its English label.

### 7.2 Alpha test, transparency, and alpha scale

Serialized alpha tests remap as:

```text
0 NEVER     1 LESS      2 EQUAL     3 LEQUAL
4 GREATER   5 NOTEQUAL  6 GEQUAL    7 ALWAYS

runtime AREF = serialized alpha_reference >> 1
```

The comparison is against the reconstructed post-texture source alpha byte,
not necessarily raw image alpha. In a normalized raw graph, compare against
`AREF / 255`. EQUAL/NOTEQUAL need byte quantization before comparison to
avoid floating-point near-equality. The executable Material path emits
`AFAIL = KEEP`: a failed alpha test writes neither framebuffer nor depth.
Other GS packet producers can still select the remaining AFAIL modes.

For hard cutouts, Blender's official Eevee guidance is to quantize alpha to 0
or 1 with a Greater Than Math node. That maps well to GREATER/GEQUAL families
when the correct post-modulation alpha and threshold are used. Other PS2 test
functions require the corresponding Compare/Math graph.

GS source-over `0x44` is close to Eevee **Blended**, but not identical in all
cases:

- GS commonly treats alpha 128 as 1.0; values above 128 can over-range before
  clamping/wrapping, while Blender opacity is normally clamped to 0-1;
- Eevee sorts transparent objects, not fragments or triangles;
- per-pass depth test/write and framebuffer masks are unavailable as ordinary
  material nodes;
- Cycles transparency is path-traced transport, not a raster framebuffer
  equation.

### 7.3 Depth, framebuffer state, wrap, and filtering

The verified remaps are:

```text
depth 0 -> NEVER
depth 1 -> GREATER
depth 3 -> GEQUAL
other   -> ALWAYS

wrap 0 -> REPEAT
wrap nonzero -> CLAMP

filter 0 -> nearest policy 0x000
filter 1 -> linear policy 0x080
filter 2 -> mip policy 0x060
filter 3..6 -> alternate mip policy 0x120
```

`z_write` controls Z masking in the generated GS state. The serialized
framebuffer selector is proved to choose framebuffer state, but a complete
value-to-FBMSK interpretation is not yet available.

The material fragment emits `FOGCOL`, `ALPHA_1`, `TEX1_1`, `ZBUF_1`,
`TEXCLUT`, `TEST_1`, `FRAME_1`, `CLAMP_1`, `TEX0_1`, and `MIPTBP1_1`.
It does not write `TEXA` or `MIPTBP2`.

Blender's Image Texture node has one extension mode for both axes. A mixed
repeat/clamp material therefore needs coordinate preprocessing, for example
`Fraction(U)` on a repeating axis and `Clamp(V,0,1)` on a clamped axis,
before the Image Texture. Merely storing the two native values as custom
properties is not a visual implementation. Likewise, collapsing all
nonzero filter policies to “Linear” loses mip behavior.

### 7.4 Culling and winding

The PS2 GS exposes primitive, depth, alpha, texture, fog, and framebuffer
state but no back-face-cull register. PCSX2's D3D11 GS device creates its
rasterizer with `D3D11_CULL_NONE`
(`ext\pcsx2\pcsx2\GS\Renderers\DX11\GSDevice11.cpp`, line 471), consistent
with culling occurring, if at all, before GS rasterization.

Killzone's native front-face convention and exact VU/EE culling path remain
unknown. `_corrected_triangle` in `native_level_io.py` flips a triangle when
its geometric cross product disagrees with averaged imported normals. This is
a useful diagnostic repair, not proof of source winding. Keep Material
**Backface Culling** disabled by default and validate silhouettes before
enabling it for any family.

## 8. Multipass, lightmaps, reflection, glow, shadows, and fog

### 8.1 What Blender can and cannot combine

For a closed stack on one surface, some ordered passes can be algebraically
folded into one node group: evaluate each layer's source RGBA, then apply the
known equation in order. This avoids Eevee object sorting. It ceases to be
exact when a pass:

- reads destination content produced by other geometry;
- relies on destination alpha, channel masks, alpha-fail writes, PABE/FBA, or
  persistent GS state;
- has a different depth test/write result;
- uses a runtime-suppressed/mutated layer;
- uses an unresolved coordinate or lighting continuation.

Duplicating geometry once per pass is closer to raster ordering for some
families, but introduces Blender ordering and equal-depth problems. It should
be an optional validation mode, not the default conversion of every
multi-layer Material.

### 8.2 Verified diagnostic families

**Street2 graffiti, Material `0x01F7E8D4`.** One `INDEX_8` texture
`0x01F70942`, UV0, operation 5 `ALPHAADD`, Z-write off, GEQUAL, alpha test
ALWAYS, repeat/repeat, scalar 255. Eleven instances contribute 404 triangles
and carry V4-8. The verified source construction is texture RGBA times vertex
RGBA divided by 128, followed by source-over. This is the strongest bounded
single-pass transparent fixture.

**Mountain Pass foliage, Material `0x00FB0B4A`.** Both layers use the same
`INDEX_8` texture `0x00561068` and UV0:

1. `LIGHTMAPCOLOR`, Z-write on, alpha GREATER, serialized reference 204
   (runtime 102);
2. `ALPHAADD`, Z-write off, alpha ALWAYS.

The two source resources have no V4-8. There are 411 placed instances and
6,576 triangle occurrences. A practical Eevee approximation is a hard-cutout
depth pass plus a blended color pass, or a single combined cutout/blend graph
when visual appearance matters more than pass fidelity. Neither should be
called exact until pass ordering and depth behavior compare against GS output.

**MASKEDWRITE foliage, Material `0x060C4C60`.** Both layers use the same
decoded texture and direct UV0:

1. `MASKEDWRITE`, Z-write on, alpha GREATER, serialized reference 204
   (runtime 102), AFAIL KEEP;
2. `ALPHAADD`, Z-write off.

The family appears on 49 instances. `MASKEDWRITE` retains the serialized
framebuffer selector's mask; in the selector-0 case it updates RGB while
preserving destination alpha. The default Blender diagnostic deliberately
does not fold the two native blend equations. It uses decoded layer-0 color
with a byte-quantized strict `alpha > 204` cutout and records the omitted
first-pass additive RGB/depth effects, second-pass low-alpha fringe,
framebuffer-alpha policy, TFX/VU source construction, and intermediate clamp.
Level 6 Approach uses this family alongside Material `0x0610B418`, a
single-layer direct-UV0 `INDEX_8` `WRITE` material covered by the bounded
layer-0 preview. Neither material requires a new texture decoder.

**Level 6 Approach water, Material `0x060C35C8`.** Four layers combine two
32-frame AnimatedTexture providers with `RGB_888` and `INDEX_8` providers.
The first animated layer is `ALPHAADD`; the second is an `ALPHA_4`
`LIGHTMAPCOLOR` layer. The final two layers use
`LIGHTMAPINTENSITY` and `MASKEDWRITE` with generated-reflection selector 10.
Every image encoding is already decoded. The visible result remains
unresolved because native frame timing/looping, generated reflection
coordinates, and destination-dependent framebuffer composition are not.

**Mountain Pass Direct 0001.** Its four records exercise layer-0 WRITE,
ALPHAADD, UV0, UV2, and an optional six-float transform. Material
`0x00564AF0` has transform:

```text
(0.9878843, -0.1551932, 0.1551936,
 0.9878841, -0.0836546, -0.0715389)
```

It is the preferred transform fixture once multiplication convention is
captured.

**Light hovertank spotlight, Material `0x0054DBAE`.** Layer 0 uses UV0
`WRITE` with the six-float transform:

```text
(0.24999996, 0.00000037, 0.00000012,
 0.99999994, 0.24999997, 0.00000008)
```

The near-zero cross terms establish the bounded axis-aligned case:
`u' = 0.25*u + 0.25`, `v' = v`. Applying that transform moves the spotlight
lens onto its authored atlas region while leaving the raw Native UV0 layer
unchanged for inspection. This proves scale/offset ordering for the
axis-aligned subset; the Mountain Pass rotation/shear fixture remains
unresolved.

**HQ Grounds V4-8 alpha multiplier.** Materials `0x0054515A` and
`0x005450C0` use indexed `ALPHAADD` color on UV0, then `ALPHA_8` operation
18 on clamped UV1. Materials `0x005452A4`, `0x005453AC`, and `0x0054D4EA`
match the same structural and per-material color-stream gate. All five have
attribute bit `0x10`, tail opcode `0x6D`, and V4-8 alpha exactly 127. The VU
color-stream continuations halve and truncate this lane to fragment alpha 63.
Fragment `0x62`,
`COMBINE_LIGHT_UNLIT_ALPHA_DIV_2`, instead supplies constant alpha 64 only
for the no-color-stream route and is not the target packet shape. TFX
HIGHLIGHT and GS blend policy `0x89` give:

```text
As = min(detailAlphaByte + 63, 255)
Cout = min(255, (Cd * As) >> 7)
```

The Blender native-reference graph uses the continuous factor `As / 128`
against layer-0 RGB and preserves its above-one brightening range. It does not
claim to reconstruct framebuffer history before layer 0 or exact byte
rounding/final channel saturation. Lighting mode 1 also uses serialized V4-8
RGB in the base preview, while complete native lighting remains a limitation.
Admission requires the exact two-layer state, a per-material V4-8 stream, and
uniform alpha 127.

**SDDocking three-pass family.** Serialized roles are UV0 base replacement,
generated reflection/view-dependent detail, and UV1 scalar mask. A retained
capture has contradictory ALPHA/TFX and texture attribution for later passes.
Treat this as runtime mutation or attribution collision, not as permission to
hard-code the captured all-MODULATE state.

### 8.3 Lightmaps and detail

UV1 is frequently suitable for a baked map or mask, but selector and operation
must decide the role. `LIGHTMAPCOLOR*`/`LIGHTMAPINTENSITY*` have proved GS
blend policies; their names alone do not prove that the provider is a static
lightmap. Mountain Pass foliage even reuses the same texture in its two
passes.

Similarly, the prevalence of `LAYERBUMPDOT3` does not establish modern
normal mapping. Keep these useful Blender approximations separate:

- **native-reference group**: exact decoded channels, proved coordinate,
  proved operation equation, no invented normal input;
- **artist lightmap group**: multiply/add a Non-Color UV1 map into Principled
  or Emission;
- **artist detail group**: optional high-frequency color/normal treatment.

### 8.4 Environment and reflection

Selectors 8-10 are generated from normals and matrices. For inspection,
Blender's Normal or Reflection vector can feed a Mapping/projection group and
the layer texture. This can demonstrate view dependence and texture role, but
the result remains a strong inference until the VU matrix/projection equation
is matched. A static UV connection is known to be wrong for these selectors.

There is no evidence that every such layer is a physically based environment
reflection. Do not automatically raise Principled Metallic or Specular.

### 8.5 Glow and emissive appearance

The game post-process uses framebuffer alpha as a glow mask, applies a palette,
downsamples/blurs it through 15 taps, and additively composites a 128x112
result. That is distinct from Material operation 15 and from a serialized
`GLOW_8` provider. The first-mission trace demonstrates the post-process but
does not establish the full material-to-mask chain.

Safe policy:

- preserve `GLOW_8` indices, dynamic-palette status, operation, and alpha
  writes;
- do not derive native Emission Strength from them;
- after a palette/material trace, approximate the post effect with an
  emission AOV and Blender compositor Glare/Fog Glow, clearly labeled;
- treat the importer's current Emission Strength `0.35` as visibility aid
  only.

### 8.6 Projective and stencil shadows

The operation enum includes `PROJECTIVESHADOW` and `STENCILSHADOW`, and the
resource model includes TextureProjector/Projector objects plus static/dynamic
shadow flags. Exact projector coordinate/state examples are not yet bounded.
A Blender projector light, camera projection, or multiply pass is therefore
an implementation experiment, not a verified mapping.

Runtime decals use the ordinary Material submission path. There is no valid
global shortcut saying “all decals blend, depth-test, and disable depth
write”; those choices remain layer state.

### 8.7 RenderZone and GS fog

The 93 RenderZones use:

```text
+0x78  f32  far_plane
+0x7C  u32  fog_enabled
+0x80  f32  fog_start
+0x84  f32  fog_end
+0x88  f32  fog_start_density
+0x8C  f32  fog_end_density
+0x90  u32  fog_color_raw
+0x94  f32  fade_distance
+0x98  s32  priority
+0x9C  u32  filter_name_string_index
```

Eighty-nine enable fog. Far plane ranges from 35 to 1,601 source units and is
not generally equal to fog end. The runtime selects the camera's zone and can
blend two zones at a boundary.

At the GS, fog is per-vertex and interpolated. When FGE is enabled, the tested
RGB equation is:

```text
out = ((F * shaded_rgb) >> 8)
    + (((255 - F) * fog_rgb) >> 8)
```

Alpha is preserved and fog occurs before framebuffer blending. This is
implemented and unit-tested in
`ext\PS2Recomp\ps2xRuntime\src\lib\gs\gs_cpu_backend.cpp:WritePixel` and
`ext\PS2Recomp\ps2xTest\src\ps2_gs_tests.cpp`.

The unknown is how RenderZone start/end/densities become each vertex F in all
paths. Recommended Blender choices:

1. **Reference approximation:** select/blend the active zone from the camera,
   map camera depth to an exposed fog factor, and Mix the fog color before a
   layer's transparency. Label the unproved density curve.
2. **Screen approximation:** use Render Layer Z/Mist in the compositor. This
   resembles distance fog but occurs after transparent composition.
3. **Art approximation:** use World or enclosing Principled Volume/Volume
   Scatter. This participates in physical light transport and is therefore
   less PS2-like, though often more useful in Cycles.

Fog is a scene/zone system. Do not duplicate an identical volume node inside
every surface material.

## 9. Practical Blender graph profiles

### 9.1 Raw GS-reference profile

Use this for validation and reverse engineering:

```text
Native UVn
  -> per-axis repeat/clamp
  -> Image Texture (numeric/Non-Color)
  -> texture-function and V4-8 byte-scale math
  -> optional byte quantization
  -> alpha-test compare
  -> proved ordered layer equation
  -> Emission
  -> Material Output
```

Requirements:

- no Principled metallic/specular lobe;
- preserve texture alpha as a separate numeric socket;
- use `255/128` where two normalized byte channels implement PS2 MODULATE;
- apply the test to reconstructed source alpha;
- expose unsupported destination-alpha/frame-mask/depth state on a visible
  “not exact” node or material property;
- use controlled color management, no exposure/look changes, and compare
  interior pixels before judging raster edges.

For a single proved WRITE layer, this can be exact up to unresolved VU
lighting and quantization. For bounded Street2 ALPHAADD it can reproduce
source construction while Eevee's framebuffer ordering remains the caveat.
For the bounded HQ Grounds operation-18 family, use detail alpha only:
`layer0.rgb * min(detailAlphaByte + 63, 255) / 128`. Do not connect detail
RGB to Normal.

### 9.2 Artist-friendly Principled profile

Use visible color textures as sRGB Base Color, scalar planes/lightmaps as
Non-Color, imported normals for ordinary diffuse lighting, and image alpha
for a proved cutout or blend. Set Metallic to zero unless the artist opts in;
choose Roughness as an explicit host setting. Principled Alpha also requires
an Eevee render method.

This profile should carry properties such as:

```text
killzone_shader_profile = "artist-approximation"
killzone_unimplemented_state = [...]
killzone_source_operation = ...
killzone_source_coordinate_selector = ...
```

It is preferable to a misleading graph that looks finished but silently
drops native passes.

### 9.3 Eevee and Cycles caveats

- Eevee **Blended** supports colored transparency but only per-object sorting,
  not per-fragment or per-triangle sorting.
- Eevee **Dithered** is suitable for hashed grayscale transparency. A Math
  comparison that outputs exactly 0/1 provides an alpha-clip graph.
- Disabling Transparency Overlap can hide sorting artifacts but changes the
  set of rendered fragments and is not a PS2 state mapping.
- Cycles can give a high-quality artistic result but cannot reproduce GS
  depth writes, destination reads, channel masks, or fixed-function blend
  order in a surface shader.
- Principled Volume/Volume Scatter is physically based fog, not the GS linear
  color interpolation above.

## 10. Current importer gap trace

| Current location | What is already correct | Gap affecting material fidelity |
|---|---|---|
| `core\native_level_material.py:NativeLevelTexture`, `NativeMaterialLayer`, `parse_level_textures`, `parse_animated_textures`, `parse_selected_materials` | Strict framing, logical token resolution, variable layer records, all known fields, optional payloads, raw bytes | Semantic runtime branches, persistent GS state, and zone/light resources remain outside this parser |
| `core\native_texture.py:_psmt8_offset`, `unswizzle_psmt8`, `unswizzle_psmt4`, `_csm1_palette_index`, `decode_csm1_rgba` | Exact observed PSMT8/PSMT4 and CSM1 decode | Dynamic `GLOW_8` palette remains unresolved |
| `native_level_io.py:_create_level_image` | Packed decoded base image, source hashes, deliberate row conversion, ALPHA as Non-Color | Native mip chain is not represented; `RGB_888` opaque alpha is host-only; no raw-reference color profile |
| `native_level_io.py:_configure_texture_node` | Nearest versus non-nearest and independent repeat/clamp handling on each axis | Filter/mip policies 1-6 still collapse to Blender's bounded nearest/linear choices |
| `native_level_io.py:_connect_layer_coordinates` | Direct selectors 0-7 use named host-corrected Native UV maps; axis-aligned six-float scale/offset is connected | Generated selectors, selector 11, and rotated/sheared transforms remain unconnected |
| `native_level_io.py:_native_material` | Every layer remains inspectable; admitted profiles connect labeled bounded or artist-approximation output and retain JSON/custom-property state | General destination-dependent multipass composition and runtime mutation remain unresolved |
| `supports_bounded_native_alphaadd_preview`, `supports_diagnostic_native_maskedwrite_alphaadd_preview`, `select_native_color_diagnostic_layer`, `enable_bounded_native_material_previews` | Narrow WRITE/ALPHAADD, structural MASKEDWRITE foliage, and color diagnostics avoid pretending all layers are solved | No general depth/framebuffer policy, exact multi-pass stack, runtime mutation support, or exact MASKEDWRITE + ALPHAADD collapse |
| `_connect_v4_8_modulation` | Correct initial texture × V4-8 × `255/128` scaling for admitted families | Cubic lighting and path-dependent V4-8 alpha are absent; alpha is host-clamped |
| Animated layer branch in `_native_material` | Retains all tokens and durations | Displays frame zero only |
| `_build_resource_mesh` and skinned adapter | Up to four host-corrected UV pairs, repeat offsets, optional V4-8, per-face Material IDs, and shared preview policies | Missing mesh lighting/shadow property use, LightZone selection, and proved front-face policy |
| `_corrected_triangle` | Produces usable diagnostic winding | Normal-guided correction is not native culling evidence |
| Whole level importer | Static render graph, selected Materials/textures, placements | RenderZone, LightZone, lights, projectors, fog controller, and post effects are not reconstructed |

Fallback Principled Metallic `0.08`, Roughness `0.72`, and diagnostic preview
Emission Strength `0.35` are host choices. They must not be cited as decoded
Killzone parameters.

## 11. Implementation status and prioritized remaining plan

### P0 — exact, low-risk foundations

1. **Implemented:** add an explicit material profile/status enum: raw
   reference, bounded native
   preview, artist approximation, or unresolved. Keep the existing raw layer
   JSON and list every ignored state.
2. **Implemented for admitted direct-coordinate paths:** create reusable node
   groups for named UV selection, per-axis repeat/clamp, PS2 `128 == 1`
   modulation, and byte-aware alpha comparisons.
3. **Implemented:** implement alpha-test functions for direct-coordinate
   layers, applying
   `reference >> 1` to reconstructed source alpha. Never silently translate
   EQUAL/NOTEQUAL as a generic threshold.
4. **Partly implemented:** split image intent between visible color and
   scalar/non-color data and retain native metadata. A dedicated raw-reference
   color-management profile and native mip presentation remain open.
5. **Pending:** animate `AnimatedTexture` through a discrete,
   duration-preserving switch after timing units/loop behavior are traced.
   Until then, report frame zero explicitly.
6. **Implemented for the bounded policies:** add synthetic Blender tests for
   mixed wrap, alpha classes, V4-8 scale, direct UV routing, and axis-aligned
   transforms before widening preview admission.

### P1 — bounded layer reconstruction

1. **Implemented for admitted layer-0 `WRITE`:** use unlit bounded output when
   coordinate, texture function, source color, alpha test, and state are
   proved.
2. **Implemented:** keep the Street2 V4-8 `ALPHAADD` family as the
   source-over reference and retain sorting/state diagnostics.
3. **Partly implemented:** retain the `0x060C4C60` strict-cutout diagnostic;
   an exact two-pass Mountain Pass foliage comparison remains pending.
4. **Ongoing rule:** implement only multipass stacks that can be closed
   algebraically without unproved destination/depth/mask behavior. Leave all
   others inspection-only.
5. **Implemented for the axis-aligned subset:** connect the light-hovertank
   scale/offset convention. Preserve rotated/sheared transforms, including
   Direct 0001, without connecting them.

### P2 — generated coordinates and scene systems

All items in this tier remain evidence-gated.

1. Trace one stable selector-8 and selector-9/10 draw through VU input,
   generated STQ, and GS output. Then build a shared environment/reflection
   node group and regression scene.
2. Parse RenderZone and LightZone records into scene metadata/volume objects.
   Implement camera-zone selection independently of surface materials.
3. Trace LightZone-to-six-coefficient selection, then reproduce the cubic
   diffuse node equation and determine when V4-8 alpha is consumed.
4. Capture the distance/density-to-fog-F function. Implement a reference
   depth mix first; keep physical volume fog as an artist option.

### P3 — unresolved variants

All items in this tier remain evidence-gated.

1. Capture the dynamic `GLOW_8` palette producer and the write path that feeds
   framebuffer alpha; then approximate the post-process in the compositor.
2. Isolate remaining operation 18/HIGHLIGHT continuations without runtime
   mutation before offering any broader detail or specular mapping.
3. Trace projector coordinate selectors and
   `PROJECTIVESHADOW`/`STENCILSHADOW` state before creating Blender lights or
   shadow receivers.
4. Resolve framebuffer selector values, FBMSK, AFAIL, PABE/FBA, runtime layer
   masks, and persistent TEXA for exact destination-dependent operations.

## 12. Validation fixtures and reference-render method

### 12.1 Local fixtures

| Fixture | Purpose |
|---|---|
| `probe-output\native-level-material-corpus-diagnostics.json` | Gate all 47-level counts, operations, selectors, flags, providers, and texture formats |
| `probe-output\level4-street2-instance-0118-material-diagnostics.json` and `.png` | Five bounded ALPHAADD materials, V4-8, INDEX_4/8, UV0, transparent sorting |
| `probe-output\level4-street2-complete-static-diagnostics.json` and preview `.blend`/`.png` | Whole-level regression and graffiti family |
| `probe-output\level10-mountainpass-complete-static-diagnostics.json` and preview `.blend`/`.png` | Foliage two-pass, direct-world opaque family, transforms, generated coordinates |
| Mountain Pass Material `0x00FB0B4A` | Alpha reference 204→102, Z-write split, same-texture ordered passes |
| Material `0x060C4C60` | MASKEDWRITE cutout/core pass, same-texture ALPHAADD fringe, 49 instances |
| Level 6 Approach Material `0x0610B418` | Bounded direct-UV0 INDEX_8 WRITE vegetation |
| Level 6 Approach Material `0x060C35C8` | Animated providers, generated reflection, and unresolved four-layer water composition |
| Light hovertank Material `0x0054DBAE` | Bounded axis-aligned six-float scale/offset convention |
| Mountain Pass Direct 0001 / Material `0x00564AF0` | Unresolved rotated/sheared six-float transform |
| SDDocking `{WRITE, LIGHTMAPCOLOR2X, LAYERBUMPDOT3}` family | Negative fixture: reject an attribution-collided capture |

Local `.blend` and image files are generated evidence and may contain
authorized game-derived data; they must remain outside distributable test
fixtures. Checked-in tests should use synthetic patterns and aggregate/hash
expectations.

### 12.2 Parser and node tests

1. Build synthetic Material blobs for every fixed field, selector-11 matrix,
   flag `0x20`, flag `0x40`, and combinations that move the next layer.
2. Test every alpha-test enum at byte values below, equal to, and above AREF,
   including odd serialized references that collapse through `>> 1`.
3. Test PSMT8/PSMT4 against the equations above for at least the existing 98
   dimension/format cases, including 8x8 row-major mips and PSMT4 nibble order.
4. Test CSM1 boundaries 7/8, 15/16, 23/24, all 16-entry CLUT values, and normal
   versus complement alpha.
5. Test mip concatenation across chunk and span boundaries; reject treating a
   chunk as one mip.
6. Render a synthetic checker with UV0-UV3, negative/tiled coordinates, every
   mixed wrap pair, nearest/linear classes, and no half-texel offset.
7. Render V4-8 values 0, 64, 128, 255 and verify the unclamped intermediate
   `texture * vertex * 255/128`.
8. Keep unsupported selectors, dynamic palettes, and destination-dependent
   state visibly unresolved rather than falling back silently.

### 12.3 Blender headless renders

Use Blender 5.2 headless with fixed engine/version, one sample where possible,
fixed resolution/camera, no exposure or look changes, and a raw numeric
Emission graph. Compare Render Result/OpenEXR values before presentation
color transforms. Maintain separate Eevee and Cycles baselines:

- Eevee is the behavior target for raster-like preview and exposes sorting
  failures;
- Cycles is only an artist-result regression, not a GS equivalence oracle.

For comparisons, report exact decoded texture hashes, maximum and mean channel
error, alpha-test coverage, and an interior-pixel result. Mask a one-pixel
raster edge unless both renderers use an identical sample convention.
Transparent tests need overlapping triangles and separate objects so that a
single convenient draw order cannot hide sorting limitations.

### 12.4 GS reference capture

`src\kz_vu.cpp:onXgkick` forwards VU1 XGKICK packets to GIF path 1.
`src\kz_gs.cpp:onGifPacket` records and forwards paths 1-3 to kzgs. Use an
authorized local run to record `KZ_GS_TRACE`, then replay with:

```text
kzgs_replay <trace> <outdir> --renderer sw \
  --dump-draws F:N --dump-rt --dump-textures
```

Use `--tex TBP:TBW:PSM:W:H[:CBP]` for PSMT8/PSMT4 plus CT32 CSM1 CLUT
extraction. The software renderer is the primary deterministic oracle;
hardware backends are a secondary cross-check. For each target draw retain:

- GIF path and sequence position;
- `PRIM`, `RGBAQ`, STQ/UV/FOG vertex data;
- `TEX0/1`, `CLAMP`, `ALPHA`, `TEST`, `ZBUF`, `FRAME`, `FOGCOL`, and relevant
  persistent state;
- input texture/CLUT hash;
- render target immediately before and after the draw.

Correlate a draw to a Core-local Material with texture payload hash, geometry,
layer order, and continuous submission evidence. If attribution is broken,
as in the retained SDDocking sample, the capture is not an acceptance
fixture.

Acceptance should proceed in layers:

1. texture and palette bytes;
2. UV/STQ samples and filtering;
3. source RGBA, V4-8, and alpha test;
4. one GS blend pass against a scripted `(A-B)*C+D` oracle;
5. ordered multipass/depth behavior;
6. view-dependent coordinates, lighting, fog, and post-process.

## 13. Remaining unknowns

The following must stay explicit in imported data and UI:

- rotated/sheared six-float transform multiplication and selector-11 matrix
  conventions;
- selectors 12-21 beyond their dispatch-level names;
- exact `lighting_mode`, alpha-source, fade, and VU-feature interactions for
  every continuation;
- LightZone selection and six cubic-light coefficients per mesh;
- path-dependent V4-8 alpha;
- framebuffer selector/FBMSK, AFAIL, PABE/FBA, runtime layer mask/mutation,
  and persistent TEXA provenance;
- native front-face convention and pre-GS culling;
- dynamic `GLOW_8` palette producer and material-to-post-glow chain;
- exact operation 16-18 visual semantics;
- projector and shadow variants;
- RenderZone density-to-vertex-fog equation;
- AnimatedTexture timing units, loop policy, and phase.

## 14. External references

All external links below were accessed **2026-10-23**.

### PlayStation 2 and GS

- PS2Docs curated manual index:
  <https://github.com/ninjadynamics/PS2Docs/tree/3a6e97416b0e23c4e94288515adef521cf077682>
- *GS User's Manual*, version 6.0:
  <https://github.com/ninjadynamics/PS2Docs/blob/3a6e97416b0e23c4e94288515adef521cf077682/GS_Users_Manual.pdf>
  (PSM/CLUT behavior, TEX0, alpha blending, tests, fog, and
  page/block/column layouts).
- PCSX2 `GSRegs.h`, pinned to the inspected revision:
  <https://github.com/PCSX2/pcsx2/blob/d3e6982a3e36647d642bcd805d3ca5b442d0975c/pcsx2/GS/GSRegs.h>
- PCSX2 GS local memory and swizzle tables:
  <https://github.com/PCSX2/pcsx2/blob/d3e6982a3e36647d642bcd805d3ca5b442d0975c/pcsx2/GS/GSLocalMemory.cpp>
  and
  <https://github.com/PCSX2/pcsx2/blob/d3e6982a3e36647d642bcd805d3ca5b442d0975c/pcsx2/GS/GSTables.cpp>
- PCSX2 CSM1 CLUT implementation:
  <https://github.com/PCSX2/pcsx2/blob/d3e6982a3e36647d642bcd805d3ca5b442d0975c/pcsx2/GS/GSClut.cpp>
- PCSX2 D3D11 GS rasterizer state (`D3D11_CULL_NONE`):
  <https://github.com/PCSX2/pcsx2/blob/d3e6982a3e36647d642bcd805d3ca5b442d0975c/pcsx2/GS/Renderers/DX11/GSDevice11.cpp>

### Blender

- Principled BSDF:
  <https://projects.blender.org/blender/blender-manual/src/branch/main/manual/render/shader_nodes/shader/principled.rst>
- Eevee material render methods, sorting limitations, and alpha-clip setup:
  <https://projects.blender.org/blender/blender-manual/src/branch/main/manual/render/eevee/material_settings.rst>
- UV Map node and named UV access:
  <https://projects.blender.org/blender/blender-manual/src/branch/main/manual/render/shader_nodes/input/uv_map.rst>
- Color Attribute RGB and alpha outputs:
  <https://projects.blender.org/blender/blender-manual/src/branch/main/manual/render/shader_nodes/input/vertex_color.rst>
- Principled Volume and Volume Scatter:
  <https://projects.blender.org/blender/blender-manual/src/branch/main/manual/render/shader_nodes/shader/volume_principled.rst>
  and
  <https://projects.blender.org/blender/blender-manual/src/branch/main/manual/render/shader_nodes/shader/volume_scatter.rst>
