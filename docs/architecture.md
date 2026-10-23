# Killzone Asset Tools Architecture

[Back to the project overview](../README.md)

This document describes the implemented architecture of Killzone Asset Tools
version 0.13.1.

Format layouts, corpus measurements, decoding equations, and detailed runtime
evidence belong in the [technical reference](./technical-reference.md). This
document focuses on how the software is organized and why its boundaries
exist.

## 1. Scope

Killzone Asset Tools is a Blender 5.2 extension and a set of companion Python
tools for read-only inspection and import of authorized Killzone disc data.

The implemented product supports:

- direct ISO 9660 and extracted DAT access;
- strict `FILES.DAT` catalog parsing;
- persistent discovery of importable maps, models, textures, and animation
  banks;
- default-LOD static map import;
- standalone static-model import;
- standalone skinned-model, Skeleton, and rigid-weight import;
- compatible native animation baking to Blender Actions;
- exact decoding of the proven native texture families;
- structural native Material nodes and bounded visible previews;
- a project-owned asset bundle format for controlled interchange tests.

The project does not currently support:

- writing edited Blender data back into native Killzone resources;
- rebuilding or repacking `FILES*.DAT`;
- collision geometry, AI, gameplay, path, effect, sound, or light import;
- full native multi-pass material reconstruction;
- runtime-generated decals;
- automatic placement of skinned actors embedded in levels;
- game or recomp mod injection.

These are architectural boundaries, not hidden or partially enabled features.

## 2. Superseded assumptions

The original plan predated direct corpus and runtime evidence. The following
assumptions have been removed:

| Legacy assumption | Current evidence and decision |
|---|---|
| Assets would be loose files or use an unknown PAK/BIG archive | The disc uses `FILES.DAT` plus `FILES01-04.DAT`; catalog paths are virtual records inside those containers. |
| A separate C++ or Rust dumper was required before Blender integration | The pure-Python core reads authorized ISOs and DAT directories directly with bounded random access. |
| A generated interchange bundle would be the primary native workflow | Native read-only import is direct. The project-owned bundle remains separate and its UI is currently hidden. |
| Blender 3.6 LTS was the target | The supported target is Blender 5.2.x; the extension manifest allows versions below 5.3. |
| Native import and export would be developed together | Native parsing is read-only. Native encoding remains blocked by explicit evidence gates. |
| Recomp asset overrides and mod packages were part of the add-on architecture | Recomp integration is outside the current product boundary. Runtime evidence may inform parsers without creating an injection pipeline. |
| Static levels could be reconstructed from `StaticMeshInstance` records alone | The verified graph also has collision-backed visual occurrences and hundreds of identity-placed direct roots. All three paths are required. |
| A decoded texture was equivalent to final game shading | Textures are providers in a larger material and runtime pipeline. Visible Blender shaders are labeled bounded or diagnostic previews. |

Calendar estimates, staffing tables, hypothetical format structures, and
unimplemented exporter sketches were intentionally removed.

## 3. Architectural principles

### 3.1 Evidence before conversion

Every native conversion must be supported by at least one of:

- corpus-wide structural validation;
- executable or recomp behavior;
- runtime capture;
- independently reproducible decoding output.

Unknown fields are preserved or reported. The importer does not invent
semantics to make an asset appear more complete.

### 3.2 Strict, bounded parsing

Native parsers validate:

- declared sizes and offsets;
- count limits and integer ranges;
- alignment and section boundaries;
- catalog/container containment;
- duplicate and ambiguous identities;
- string indices and optional UTF-8 decoding;
- finite numeric values;
- exact packet and payload framing.

Malformed archive structure is fatal. Unsupported per-asset candidates may be
recorded as scan issues so other independently valid assets remain available.

### 3.3 Native formats are read-only

No native serializer is registered. An imported mesh, image, Action, or
Material cannot be exported to `Core`, `CoreStream`, or `FILES*.DAT`.

The project-owned bundle format is not a native Killzone format. Its
round-trip guarantees do not imply native encoder support.

### 3.4 Blender isolation

Modules under `io_scene_killzone/core/` do not import `bpy`. They return
immutable Python data structures and explicit errors.

Blender-facing adapters live at the package root:

- `native_level_io.py`
- `native_static_io.py`
- `native_skinned_io.py`
- `native_animation_io.py`
- `native_texture_io.py`
- `scene_io.py`

This separation keeps format tests runnable under ordinary Python.

### 3.5 Preserve source identity

Imported datablocks retain source paths, object tokens, directory identities,
hashes, format names, counts, and policy decisions as custom properties.
Packed images and fake-user Actions survive saving and reopening a `.blend`.

## 4. System context

```text
Authorized ISO or extracted DAT directory
                    |
                    v
        ISO 9660 / DAT archive source
                    |
                    v
          FILES.DAT virtual catalog
                    |
          +---------+----------+
          |                    |
          v                    v
 Persistent asset cache   Direct asset loaders
          |                    |
          v                    v
   KZ Tools selectors     Pure native parsers
          |                    |
          +---------+----------+
                    |
                    v
              Blender adapters
                    |
                    v
 Collections, Meshes, Armatures, Materials,
 packed Images, Empties, and slotted Actions
```

Companion command-line tools use the same pure core and bypass Blender.

## 5. Dependency direction

```text
operators.py
    |
    +--> native_*_io.py / scene_io.py
    |          |
    |          +--> core/*
    |
    +--> core/native_asset_catalog.py

tools/*
    |
    +--> core/*

tests/test_*.py --> core/*
tests/blender_smoke.py --> Blender adapters + core/*
```

The core must never depend on Blender adapters or operators. Tools must not
duplicate parser logic that belongs in the core.

## 6. Archive and container layer

### 6.1 ISO 9660

[`iso9660.py`](../io_scene_killzone/core/iso9660.py) provides bounded ISO 9660
record discovery and file-range reads. It validates redundant endian fields,
logical block sizes, volume bounds, directory depth, record counts, and file
extents.

The parser does not mount or extract the whole disc.

### 6.2 DAT catalog

[`killzone_dat.py`](../io_scene_killzone/core/killzone_dat.py) parses the
recursive metadata stored at the start of `FILES.DAT`. It resolves each
virtual path to:

- package index;
- container name;
- byte offset;
- byte size;
- raw catalog flags.

Path collisions, cycles, orphan records, unsafe names, and out-of-container
ranges are rejected.

### 6.3 Unified archive source

[`archive_source.py`](../io_scene_killzone/core/archive_source.py) exposes the
same interface for:

- an ISO containing all five containers; or
- a directory containing `FILES.DAT` and `FILES01-04.DAT`.

Consumers request catalog entries or bounded ranges. They do not need to know
whether bytes came from an ISO extent or an extracted file.

## 7. Persistent asset discovery

[`native_asset_catalog.py`](../io_scene_killzone/core/native_asset_catalog.py)
performs the manual first scan exposed in add-on preferences.

The cache stores:

- resolved source path and source type;
- container sizes;
- naturally ordered map entries and display labels;
- standalone texture entries;
- uniquely selectable static and skinned model roots;
- animation-bank paths, clip counts, and lightweight clip identities,
  durations, and playback modes;
- explicit issues for unsupported candidates.

The cache is serialized as versioned JSON in `AddonPreferences`. It is reused
across Blender files. Changing the configured source path invalidates the
active cache until the user rescans.

The validated NTSC-U scan used for version 0.13.1 produces:

| Cached category | Count |
|---|---:|
| Maps | 47 |
| Standalone textures | 19 |
| Static model roots | 8 |
| Skinned model roots | 108 |
| Total model roots | 116 |
| Animation banks | 352 |
| Unsupported model-graph issues | 2 |

Both issues are ambiguous graph identities in `allweapons.default.core`. They
are reported rather than guessed. Its valid animation bank remains available.

Counts are corpus-dependent and are not hard-coded admission requirements.

### 7.1 Map naming and ordering

Campaign maps are sorted by numeric chapter and then area, so chapter 10 does
not appear after chapter 1. Display labels combine the retail chapter name
with a human-readable internal area name.

Multiplayer maps follow campaign maps and use the eight retail map names.

### 7.2 Model-root discovery

A Core can contain multiple model roots. Cache entries therefore identify a
model by:

- archive path;
- model kind;
- root object token;
- section offset;
- object index.

Duplicate root tokens are not selectable. Different appearance roots from the
same character Core remain separate entries until stronger naming evidence is
available.

## 8. Generic Core framing

[`core_format.py`](../io_scene_killzone/core/core_format.py) validates the
fixed Core header and string table. Strings retain raw bytes because not every
entry is valid UTF-8.

[`native_level.py`](../io_scene_killzone/core/native_level.py) contains the
generic variant-5 typed-section walker used across levels, models, materials,
Skeletons, and animations. It recognizes:

- expanded object directories;
- compact instance directories;
- animation BoneMap directories;
- object-owned CoreStream ranges.

`NativeObjectKey(section_offset, object_index)` is the stable directory
identity. A 32-bit object token alone is not always unique.

The current validated corpus contains:

| Structure | Count |
|---|---:|
| Catalog files | 11,569 |
| Catalog directories | 1,163 |
| Core resources | 653 |
| Paired CoreStream resources | 514 |
| Variant-1 self-contained Cores | 139 |

## 9. Domain parsers

### 9.1 Static geometry

[`native_static_geometry.py`](../io_scene_killzone/core/native_static_geometry.py)
decodes the verified DMA/VIF packet families into positions, normals, ADC
state, UV sets, optional V4-8 color, auxiliary bytes, and triangle strips.

[`native_level.py`](../io_scene_killzone/core/native_level.py) builds graphs of:

- `RegularStaticMeshResource`;
- `MultiStaticMeshResource`;
- `LodStaticMeshResource`.

Multi nodes expand all children. LOD nodes select child zero under the current
default-LOD policy.

The measured level corpus contains 9,327 regular resources and 164,630 packets
across 18 packet-tail shapes.

[`native_static_mesh.py`](../io_scene_killzone/core/native_static_mesh.py)
retains the earlier narrow leading-root packet-directory validator. The
generic static geometry path is used for current Blender imports.

### 9.2 Level graph

A level is not a flat mesh. The render plan contains:

- direct graph roots placed at identity;
- explicit `StaticMeshInstance` occurrences with source matrices;
- explicit `CollisionMeshInstance` visual occurrences with source matrices;
- `SimpleAnimatingSkinnedMeshInstance` metadata;
- shared resource leaves and wrapper expansion.

The verified 47-level graph has:

- 5,896 static resource roots;
- 3,158 roots referenced by static instances;
- 1,985 roots referenced by collision-mesh instances;
- 753 direct world roots;
- 813 default-LOD direct leaf occurrences;
- 22,109 default-LOD static-instance leaf occurrences;
- 15,928 default-LOD collision-mesh-instance leaf occurrences.

Importing only explicit instances would omit most stored world geometry.

### 9.3 Native textures

[`native_texture.py`](../io_scene_killzone/core/native_texture.py) decodes the
19 proven self-contained variant-1 PSMT8/CSM1 textures.

[`native_level_material.py`](../io_scene_killzone/core/native_level_material.py)
handles embedded variant-5 texture definitions, including:

- `INDEX_4`;
- `INDEX_8`;
- `ALPHA_4`;
- `ALPHA_8`;
- `RGBA_8888`;
- `RGB_888`;
- animated texture references;
- normal and complement palette candidates.

The full corpus has 13,891 physical variant-5 texture definitions. The 47
levels contain 12,566 logical texture tokens.

### 9.4 Materials

Native Materials are parsed structurally into ordered layers and preserved
state. Each layer records its provider, operation, coordinate selector, wrap,
filter, test, blend-related fields, palette policy, optional transforms, and
opaque bytes.

The architecture separates:

1. exact provider decoding;
2. proven coordinate routing;
3. bounded visible preview policy;
4. unresolved final native composition.

This prevents a decoded image from being mistaken for an exact final shader.

### 9.5 Skeletons and skinned geometry

[`native_skeleton.py`](../io_scene_killzone/core/native_skeleton.py) parses the
packed Skeleton hierarchy, names, attachments, bounds, categories, and
serialized temporary transforms.

[`native_skinned_geometry.py`](../io_scene_killzone/core/native_skinned_geometry.py)
decodes regular skinned DMA/VIF packets and selector-to-bone assignments.

[`native_skinned_model.py`](../io_scene_killzone/core/native_skinned_model.py)
discovers graph roots, expands Multi nodes, selects LOD0, resolves one
Skeleton, and selects the required materials and textures.

The importer currently creates one source-order bone influence at weight 1.0
per target. Blend shapes and linked static resources are not connected.

The standalone importer interprets serialized temporary transforms as
row-vector global inverse-bind matrices, transposes them for Blender, and
inverts them to construct a candidate rest pose. This interpretation is
recorded as diagnostic metadata.

### 9.6 Animations

[`native_animation.py`](../io_scene_killzone/core/native_animation.py) decodes
packed tracks, all nine time modes, fixed and adaptive translation, scale, all
verified rotation codecs, and retail VU0 interpolation behavior.

[`native_animation_resource.py`](../io_scene_killzone/core/native_animation_resource.py)
parses BoneMaps and clip objects, binds track names to Skeleton bones, samples
native poses, and preserves clip policy.

The Blender batch policy admits a clip only when its binding covers every
source Skeleton bone. Extra bank tracks may remain unmatched and are recorded.

### 9.7 Decals

[`native_decal.py`](../io_scene_killzone/core/native_decal.py) preserves the
11 verified shared `DecalResource` policies and pure runtime-policy helpers.

Projected decal geometry is runtime-generated and is not fabricated during
level import.

## 10. Blender adapter layer

### 10.1 Map import

[`native_level_io.py`](../io_scene_killzone/native_level_io.py) converts a
`NativeLevel` into:

```text
Killzone <map>
|-- Direct World (identity)
`-- Props
    |-- Static Instances
    |-- Collision Mesh Instances
    `-- Animated Skinned Instances (metadata only)
```

Repeated occurrences remain distinct Blender objects while sharing exact
decoded mesh datablocks. A collision-mesh occurrence uses its reflected
`General.MeshResource` for visible geometry and retains the companion
collision-resource identity as metadata; collision geometry is not decoded.

When **Import Map Textures** is disabled, the core skips Texture,
AnimatedTexture, and Material decoding. Meshes use a diagnostic fallback
material.

When enabled, selected images are decoded, packed, and referenced by native
Material nodes.

### 10.2 Static model import

[`native_static_io.py`](../io_scene_killzone/native_static_io.py) imports one
uniquely selected static graph root into a dedicated collection. It uses the
same LOD0 expansion, packet decoder, materials, packed images, and preview
policy as map geometry.

### 10.3 Skinned model import

[`native_skinned_io.py`](../io_scene_killzone/native_skinned_io.py) creates:

- a source-order Armature;
- LOD0 mesh parts;
- one vertex group assignment per target;
- Armature modifiers;
- selected native Materials and packed images;
- source and validation metadata.

Static and skinned model adapters use the same host-corrected direct-UV
routing, bounded material-preview policies, and packed-image path as map
geometry.

### 10.4 Animation baking

[`native_animation_io.py`](../io_scene_killzone/native_animation_io.py)
converts sampled native poses into Blender 5.2 slotted Actions.

Actions preserve:

- source clip token and directory key;
- playback mode and event token;
- BoneMap identity;
- bake and scene rates;
- unmatched track names;
- root-trajectory policy;
- maximum matrix decomposition residual;
- Core and CoreStream hashes.

The sidebar builds a cached multi-select list from the model Core and
optionally one explicit shared bank. All clips are selected by default for
backward-compatible behavior, while **All** and **None** controls make bounded
imports explicit. Libraries with no selected clips are not loaded. The Action
writer, legacy retry policy, and decomposition tolerance are persistent add-on
preferences rather than per-scene sidebar controls.

Two Action writers share the same native sampling and decomposition stage:

- **Optimized Bulk FCurves** creates the slot, layer, strip, channel bag, and
  FCurves directly; writes coordinates and linear interpolation with
  `foreach_set`; and reduces exactly constant channels to one key.
- **Legacy Keyframe Insert** retains the original per-pose-bone
  `keyframe_insert` implementation as a known-working fallback.

Automatic fallback retries only Blender bulk-writing failures. Native decode
or decomposition failures are not retried because both writers consume the
same validated channels. Actions record the writer used, scalar keyframe
count, FCurve count, and fallback totals.

The preference-level residual tolerance defaults to `0.05`. Sampled-track
motion is the default; total-distance root reconstruction is a separate
opt-in API policy.

### 10.5 Standalone texture import

[`native_texture_io.py`](../io_scene_killzone/native_texture_io.py) creates a
persistent packed Blender Image with source and decoded hashes. The dedicated
sidebar selector is not exposed; map and model imports create their referenced
images automatically.

### 10.6 Project-owned bundle adapter

[`scene_io.py`](../io_scene_killzone/scene_io.py) imports and exports version-1
project-owned bundles.

The bundle path can:

- preserve an unchanged `.kzm` mesh byte-for-byte;
- preserve attached original blobs byte-for-byte;
- write edited project-owned `.kzm` data;
- retain unknown interchange chunks.

It cannot encode edited native Killzone data. Bundle operators are currently
not registered in the UI.

## 11. Material preview architecture

Every serialized native layer remains inspectable. Visible output is connected
only through named preview policies.

### 11.1 Preview policies

The current policies are:

- `layer0-write`: bounded direct-UV layer-0 `WRITE`, including proved
  axis-aligned six-float scale/offset transforms;
- `single-layer-*-alphaadd-v4-8`: exact admitted indexed `ALPHAADD` with
  Native V4-8 modulation;
- `two-layer-alphaadd-layerbumpdot3`: bounded direct-UV0/UV1 HQ Grounds
  composition using layer-1 alpha as a framebuffer multiplier;
- `two-layer-maskedwrite-alphaadd-cutout`: artist-approximation foliage
  preview using a byte-quantized strict decoded-alpha cutout while preserving
  both native passes and their unresolved framebuffer behavior;
- `diagnostic-color-layerN`: structurally selected decoded provider color;
- `diagnostic-v4-8-color-layerN`: decoded provider color modulated by
  serialized Native V4-8 outside exact depth/blend admission.

General unsupported multi-layer composition remains disconnected.

### 11.2 V4-8 modulation

For admitted source-color paths, the preview uses:

```text
RGB   = texture.rgb * vertex.rgb * (255 / 128)
alpha = clamp(texture.a * vertex.a * (255 / 128))
```

This reflects the measured convention that source value 128 represents 1.0.

HQ Grounds Material `0x0055A49C` demonstrates why provider and final color
must remain separate. Texture `0x0055A886` is uniformly opaque white, while
serialized V4-8 is `(37, 36, 32, 128)`. A texture-only diagnostic renders
white; the V4-8 diagnostic produces the evidenced dark gray-brown tint. Its
`z_write=1` state remains outside exact `ALPHAADD` admission.

### 11.3 Bounded HQ Grounds layer multiplier

HQ Grounds target Materials `0x0054515A` and `0x005450C0` admit a separate
narrow two-layer policy. The same structural gate also admits `0x005452A4`,
`0x005453AC`, and `0x0054D4EA`. Layer 0 is indexed `ALPHAADD` on UV0. Layer
1 is `ALPHA_8` `LAYERBUMPDOT3` on clamped UV1 with TFX policy 3. Their
packets do carry Native V4-8, with alpha exactly 127. The VU color-stream
alpha-divide-by-two continuations truncate that lane to fragment alpha 63;
the no-color-stream fragment `0x62` and its constant alpha 64 do not apply to
these records. GS HIGHLIGHT and blend policy `0x89` reduce to:

```text
fragmentAlphaByte = trunc(127 / 2) = 63
sourceAlphaByte = min(detailAlphaByte + 63, 255)
previewRGB = layer0RGB * sourceAlphaByte / 128
```

The detail layer is a scalar mask, not a normal map. The Blender graph keeps
factors above one so the native brightening range remains visible. It cannot
reproduce framebuffer content that existed before layer 0, nor exact GS byte
quantization, final channel saturation, or the complete native lighting term.
Lighting mode 1 applies the serialized V4-8 RGB modulation to the base
preview. Materials with a different V4-8 alpha range, different state,
generated coordinates, optional transforms, or extra layers are not admitted.

### 11.4 Unresolved material behavior

The following remain unresolved or only partially bounded:

- general multi-pass composition;
- generated coordinate routes;
- rotated or sheared six-float transform convention;
- dynamic GLOW palette selection;
- runtime Material mutation;
- complete PS2 alpha and framebuffer behavior outside captured families;
- exact native lighting for every material family.

Preview metadata must state these limits.

## 12. Blender UI and preferences

[`operators.py`](../io_scene_killzone/operators.py) owns registration,
preferences, dynamic cached selectors, operators, and the **KZ Tools** panel.

The UI has two import sections:

- **Import Map** — full static render graph plus authored prop placements
- **Import Model** — standalone definition/model preview without map placement

ISO or DAT selection and animation writer settings are intentionally located
in add-on preferences rather than per scene.

The additional animation-bank selector uses Blender's searchable popup and
includes each exact `.core` filename in its label. Third-person character
models with no model-Core clips show a bounded hint to try
`allweapons.default.core`; vehicle and turret paths are excluded from that
heuristic.

The scan is manual because it reads and validates hundreds of Core headers and
selected object directories. Cache schema 2 also records lightweight clip
metadata so model and bank selection can rebuild the checklist without
decoding animation payloads. The resulting cache avoids repeating that work
for every Blender file. Upgrading from schema 1 requires one rescan.

Bundle import/export operators are hidden until that workflow is ready for
normal users.

## 13. Error and diagnostic model

Expected native-data failures derive from `KZAssetError`.

Rules:

- archive-integrity failures cancel the operation;
- changed archive sources invalidate cached selections;
- stale cache indices require a rescan;
- unsupported candidate graphs become explicit cache issues;
- importers do not return success-shaped placeholders for malformed assets;
- animation clips above the configured residual are recorded as failures;
- incompatible clips are counted and skipped explicitly;
- unresolved material state is represented in nodes and custom properties.

Important custom properties include:

- source paths and hashes;
- object and resource tokens;
- section offsets and object indices;
- packet, triangle, and vertex counts;
- texture format and decode status;
- material preview mode and shader status;
- Skeleton and root tokens;
- animation residual and unmatched track data.

## 14. Companion tools

The scripts under [`tools/`](../tools/) exercise the same core without
requiring the sidebar workflow.

| Tool | Role |
|---|---|
| `kz_asset_probe.py` | ISO/container inventory, hashing, and private extraction |
| `kz_catalog.py` | Full virtual catalog validation and reporting |
| `kz_core_scan.py` | Core header, variant, and first-type inventory |
| `kz_static_mesh_scan.py` | Narrow leading-root static packet-directory validation |
| `kz_level_preview.py` | Reproducible map import, render, and texture diagnostics |
| `kz_skinned_preview.py` | Reproducible skinned model, rig, texture, and Action diagnostics |
| `kz_decal_probe.py` | Shared decal-policy reporting |

Tools write reports and previews supplied by the user. No game data is shipped
with the repository.

## 15. Validation architecture

### 15.1 Pure-Python tests

The `tests/test_*.py` suite covers:

- ISO and DAT bounds;
- catalog validation;
- Core framing;
- static packet families and graph expansion;
- texture and material parsing;
- Skeleton and skinned packet decoding;
- packed animation codecs and sampling;
- decal policies;
- cache serialization, map labels, and ordering.

At version 0.13.1 the suite contains 86 passing tests.

### 15.2 Blender smoke test

[`blender_smoke.py`](../tests/blender_smoke.py) validates actual Blender 5.2
behavior, including:

- bundle import/edit/export;
- native image creation and packing;
- static and skinned source-V-down to Blender-V-up UV conversion, including
  authored repeat-tile offsets;
- axis-aligned six-float UV transforms, mixed-axis wrap, and native alpha-test
  comparisons;
- map collection and instance structure;
- geometry-only map policy;
- static model import;
- Armature, weights, and deformation;
- legacy and optimized Action baking, selected-clip batching, constant-channel
  reduction, automatic fallback, and repeat policy;
- exact, bounded, and diagnostic material preview graphs, including the
  HQ Grounds layer multiplier and MASKEDWRITE foliage cutout;
- add-on-preference animation settings and the shared-bank character hint.

Fixtures are generated procedurally and contain no game data.

### 15.3 Authorized corpus validation

Real-disc validation is run separately because game data cannot be committed.
JSON diagnostics, counts, and screenshots may be retained locally, but the ISO
and DAT containers remain private.

Corpus claims must be reproducible with documented tools and selectors.

## 16. Repository structure

```text
io_scene_killzone/
|-- __init__.py
|-- blender_manifest.toml
|-- operators.py
|-- native_level_io.py
|-- native_static_io.py
|-- native_skinned_io.py
|-- native_animation_io.py
|-- native_texture_io.py
|-- scene_io.py
`-- core/
    |-- archive_source.py
    |-- iso9660.py
    |-- killzone_dat.py
    |-- core_format.py
    |-- native_asset_catalog.py
    |-- native_level.py
    |-- native_static_geometry.py
    |-- native_static_model.py
    |-- native_level_material.py
    |-- native_texture.py
    |-- native_skeleton.py
    |-- native_skinned_geometry.py
    |-- native_skinned_model.py
    |-- native_animation.py
    |-- native_animation_resource.py
    `-- native_decal.py

tools/
tests/
docs/
|-- architecture.md
`-- technical-reference.md
```

## 17. Architectural decisions

| Decision | Reason |
|---|---|
| Read directly from ISO or DAT directories | Avoids mandatory extraction and keeps one validated source abstraction. |
| Keep `core/` free of `bpy` | Enables fast deterministic tests and reuse by CLI tools. |
| Cache discovery in add-on preferences | Scanning is source-wide, while selections are used across Blender files. |
| Identify objects by directory key plus token | Tokens are not always unique within a Core. |
| Expand Multi and select LOD child zero | Matches the current bounded default-LOD policy. |
| Import direct roots plus static and collision-backed visual instances | Required by the measured level render graph. |
| Pack decoded images | Blender pixel buffers otherwise may not survive reopening. |
| Use one rigid weight per skinned target | Matches the verified selector semantics. |
| Require full Skeleton coverage for automatic batch clips | Avoids silently binding unrelated animation banks. |
| Cache lightweight clip metadata, not decoded tracks | Enables instant checklist updates without making preference changes parse animation payloads. |
| Default to bulk FCurve writing with legacy fallback | Reduces Python/Blender key insertion overhead while retaining the established writer for compatibility. |
| Expose animation residual tolerance | Blender channels cannot represent arbitrary affine pose matrices. |
| Label material output as exact, bounded, or diagnostic | Prevents preview convenience from becoming a format claim. |
| Keep native export unavailable | No unchanged native serializer, edited encoder, archive repacker, or game-side acceptance proof exists. |

## 18. Evidence-gated next steps

Work is prioritized by evidence gates rather than calendar phases.

### 18.1 Discovery and workflow

- add search and filtering for large model and animation-clip lists;
- derive stronger appearance names when serialized naming evidence is found;
- add scan progress and cache diagnostics without weakening parser strictness.

### 18.2 Materials

- capture and reproduce additional GS-state families;
- prove generated-coordinate and rotated/sheared optional-transform
  equations;
- determine dynamic GLOW palette sources;
- separate more exact families from diagnostic color policies;
- validate material output against bounded runtime captures.

### 18.3 Level systems

- connect `SimpleAnimatingSkinnedMeshInstance` records to standalone model and
  animation import;
- prove proximity-parent transform composition;
- decode collision-resource geometry and add gameplay-object inspection only
  after their layouts and semantics are independently bounded;
- preserve higher-LOD data without selecting it by default.

### 18.4 Skinned models and animations

- connect supported linked-static and blend-shape resources;
- strengthen inverse-bind/rest-pose evidence;
- improve per-clip compatibility explanations and shared-bank discovery;
- expand real-corpus acceptance tests for affine residual policies.

### 18.5 Native writing

Native export remains blocked until all of the following are independently
demonstrated:

1. byte-identical parse/serialize of unchanged native objects;
2. complete preservation of unknown fields, padding, and object directories;
3. safe encoding of edited geometry, textures, Skeletons, and animations;
4. Core and CoreStream relocation and alignment;
5. DAT catalog and container repacking;
6. game-side or recomp-side acceptance tests;
7. failure recovery that cannot corrupt source data.

Until then, native source data remains authoritative and immutable.

## 19. References

- [Project overview](../README.md)
- [Technical reference](./technical-reference.md)
- [Map prop instancing research](./map_prop_instancing_research.md)
- [Material and shader research](./material_shader_research.md)
- [Blender extension manifest](../io_scene_killzone/blender_manifest.toml)
- [Pure-Python tests](../tests/)
- [Companion tools](../tools/)
