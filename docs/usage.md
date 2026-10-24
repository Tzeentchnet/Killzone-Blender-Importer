# Usage guide

This guide covers the Blender extension's asset import workflows, material
preview options, animation selection, troubleshooting, and companion tools.

## Asset bundles

Version 1 Killzone Asset Bundle parsing remains implemented, but its Blender
import/export controls are temporarily hidden while that workflow is refined.
A bundle contains a manifest, project-owned `.kzm` meshes, material metadata,
and optional preserved original blobs.

See [Bundle layout](./technical-reference.md#bundle-layout) for the on-disk
structure and integrity rules.

## Native assets

Configure native assets once:

1. Open **Edit > Preferences**, expand **Killzone Asset Tools**, and choose an
   authorized Killzone ISO or a directory containing `FILES.DAT` and
   `FILES01-04.DAT`.
2. Select **Scan / Rescan Importable Assets**. The resulting map, model, and
   animation-bank catalog is cached in the add-on preferences and reused
   across Blender files.
3. Open **3D View > Sidebar > KZ Tools** to import from the cached selectors.

**Import Map** lists campaign maps by retail chapter and area, followed by
multiplayer maps, using numeric rather than lexical level ordering. **Import
Map Textures** is enabled by default; disable it for a geometry-only import
that skips native texture and material decoding.

**Map prop instancing.** Map import reconstructs all three verified visual
paths: direct world roots, ordinary static instances, and collision-backed
visual instances. It recursively expands Multi resources, selects default
LOD0, and applies each occurrence's authored matrix. Repeated occurrences are
separate Blender objects at their intended placements but share decoded mesh
datablocks. Collision resources remain attached as provenance metadata;
collision geometry itself is not decoded.

**UV and texture handling.** Enabled texture imports decode and pack native
images into the Blender file. Direct native UV layers are converted from
source V-down coordinates to Blender V-up coordinates without discarding
authored repeat-tile offsets. UV0 and UV1 remain distinct, and bounded
axis-aligned six-float material transforms apply their serialized scale and
offset in shader space. This covers verified atlas transforms used by model
sub-materials; rotated or sheared transforms remain diagnostic rather than
being guessed.

**Additional material coverage.** Proven native texture decoding now covers
`INDEX_4`, `INDEX_8`, `ALPHA_4`, `ALPHA_8`, `RGBA_8888`, and `RGB_888`.
Bounded visible material previews cover direct `WRITE`, serialized Native
V4-8-modulated `ALPHAADD`, the two-layer
`ALPHAADD + LAYERBUMPDOT3` family, and the same-texture
`MASKEDWRITE + ALPHAADD` foliage family. `LAYERBUMPDOT3` is treated as the
verified scalar/light layer rather than incorrectly connected as a normal
map. The foliage preview uses a strict decoded-alpha `> 204` cutout and is
labeled as an artist approximation because the native additive framebuffer
contribution and low-alpha fringe are not reconstructed. Unsupported complex
multi-pass shaders remain clearly diagnostic instead of being fabricated.

Use Blender's **Material Preview** or **Rendered** viewport shading to see
imported textures; **Solid** shading does not display shader images.

**Material Preview options.** The sidebar's **Material Preview** box applies
to both map and model imports:

- **Use Standard View Transform** (default on) switches the scene to the
  Standard view transform after import. Previews are unlit Emission, and
  Blender's default AgX transform would otherwise compress and desaturate
  them.
- **Artist Lightmap Layers** (default off) composes the `LAYERBUMPDOT3`
  scalar layer, usually baked shading on UV1, over admitted `WRITE` base
  textures using the GS HIGHLIGHT/`0x89` equation. It is labeled an artist
  approximation until a GS capture confirms it for this family.
- **Artist Fallback Textures** (default off) shows the first decoded color
  layer on materials that otherwise keep the diagnostic object color.

Vertex-color (V4-8) and LAYERBUMPDOT3 products are evaluated in display-gamma
space, matching how the GS multiplies display-encoded bytes. Failed imports
remove any partially created datablocks, and each map records per-phase
timings in the root collection's `killzone_import_timings_json` property.

**Import Model** combines standalone static props and supported skinned model
roots. Skinned models include their source-order Skeleton and rigid vertex
assignments. Static and skinned model materials use the same bounded decoded
texture previews and host-corrected direct-UV policy as map meshes. Cached
clips from the model Core and one optional shared bank are shown as a
multi-select checklist. All clips remain selected by default to preserve the
earlier behavior; use **None** and select only the Actions needed for a faster
import. Use **Search Banks...** to filter the animation-bank catalog by its
exact `.core` filename or friendly name instead of scrolling the complete
list. Only clips that cover the model's complete Skeleton are admitted.
Third-person character models without model-Core clips show a hint to try
`allweapons.default.core` for shared Actions. A standalone static model is a
definition preview and has no map placement context; use **Import Map** to
create every authored prop occurrence at its serialized transform. Map
occurrences remain distinct Blender objects while safely sharing immutable
mesh datablocks.

Animation import settings are in **Edit > Preferences > Add-ons > Killzone
Asset Tools**. The default **Optimized Bulk FCurves** writer creates Blender
5.2 layered Actions with vectorized keyframes and reduces exactly constant
channels to one key. **Legacy Keyframe Insert** keeps the original
known-working writer available. Automatic fallback to that legacy writer is
enabled by default if the optimized Blender-writing stage fails. Native
decoding, pose evaluation, and decomposition validation are shared by both
writers. The animation decomposition tolerance defaults to `0.05`.

Version 0.14.0 uses asset-cache schema 2 to store lightweight clip identities
and durations. Run **Scan / Rescan Importable Assets** once after upgrading.

For example, `entity.player.3rd.luger.core` contains 174 compatible clips. On
the validation machine, importing all of them improved from an estimated
46 seconds with the legacy writer to about 18.5 seconds with the optimized
writer. Selecting one clip creates one Action and avoids parsing or baking the
other 173 clips.

When a one-clip Core provides matching serialized Skeleton and root-track
names, such as `skelet_Collapsing_tank` and `Collapsing_tank_joint`, the Action
uses the semantic name `Collapsing_tank`. Ambiguous multi-clip banks retain
their source path, token, and directory-key title. Every Action records its
requested and final Blender names, naming source, and former coded title in
the `killzone_action_name_requested`, `killzone_action_name`,
`killzone_action_name_source`, and `killzone_legacy_action_name` custom
properties.

Changing the ISO or DAT directory invalidates the active cache until
**Scan / Rescan Importable Assets** is run again.

## Material preview troubleshooting

Native textures can be provider data rather than the final visible color. For
example, `level.1.hqgrounds` Material `0x0055A49C` references 128x128
`INDEX_8` Texture `0x0055A886`, whose decoded pixels are uniformly opaque
white. Its authored preview tint comes from serialized Native V4-8 vertex
color `(37, 36, 32, 128)`.

For any material that looks incorrect, inspect these custom properties on the
Blender Material:

- `killzone_core_local_token`
- `killzone_preview_mode`
- `killzone_shader_profile`
- `killzone_shader_status`
- `killzone_shader_unimplemented_state_json`
- `killzone_preview_v4_8_modulation`
- `killzone_preview_modulation_space`

## Companion tools

The companion tools in [`tools/`](../tools/) also read from an authorized ISO
or extracted DAT directory. They cover catalog inspection, Core scanning,
textures, static levels, skinned characters, animation banks, and bounded
diagnostics.

Common entry points include:

- [`kz_asset_probe.py`](../tools/kz_asset_probe.py) for disc/container
  inventory
- [`kz_catalog.py`](../tools/kz_catalog.py) for native catalog reporting
- [`kz_core_scan.py`](../tools/kz_core_scan.py) for Core inventory
- [`kz_level_preview.py`](../tools/kz_level_preview.py) for experimental
  static level imports
- [`kz_skinned_preview.py`](../tools/kz_skinned_preview.py) for standalone
  characters and animation Actions

Exact commands, selection rules, validation policies, and report fields are
documented in the [technical reference](./technical-reference.md).
