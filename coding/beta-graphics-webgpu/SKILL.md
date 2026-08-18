---
name: beta-graphics-webgpu
description: Build compact, high-value WebGPU/WebGL2 scientific visualizations for scalar/vector fields, derivatives, ray-isosurfaces, volume integration, procedural lighting, and GPU-compute dynamics.
---

# WebGPU / WebGL Field Visualization Skill

Use this skill when an agent must create detailed, performant, inspectable scientific or mathematical graphics in a browser without relying on a large rendering engine.

## Core design doctrine

1. Prefer **WebGPU** for new work: render + compute share the same device, resources, synchronization model, and WGSL codebase. Keep **WebGL2** as a rendering fallback when reach matters.
2. Build one field abstraction and derive everything else from it. A model should expose `(scalar, vector)` at position `p` and time `t`; gradient, divergence, curl, and Laplacian are generic operators over that abstraction.
3. Make visualization modes consumers of the same field API: slice, isosurface, volume, glyphs, streamlines, probes, and dynamics should not each reinvent model logic.
4. Batch views. One canvas plus viewport/scissor rectangles is usually more compact and cheaper than N canvases/contexts. Per-view data belongs in aligned uniform blocks; shared camera/light/time data belongs in a frame block.
5. Keep interactive state declarative. Model ID, quantity ID, color-map ID, transfer-function ID, iso value, density, slice, range, and glyph style are data, not shader variants. Avoid pipeline proliferation unless specialization materially improves performance.
6. Let the GPU own evolving fields. Ping-pong textures or storage buffers between frames; submit compute before rendering in the same command encoder when possible.

## Current API baseline — August 2026

Primary references:

- WebGPU Candidate Recommendation Draft (12 Aug 2026) / living editor draft: https://www.w3.org/TR/webgpu/ and https://gpuweb.github.io/gpuweb/
- WGSL Candidate Recommendation Draft / current technical report: https://www.w3.org/TR/WGSL/
- WebGL 2.0: https://registry.khronos.org/webgl/specs/latest/2.0/

Use the contemporary WebGPU initialization path:

```js
const adapter = await navigator.gpu.requestAdapter({
  powerPreference: "high-performance",
});
const device = await adapter.requestDevice();
const context = canvas.getContext("webgpu");
const format = navigator.gpu.getPreferredCanvasFormat();
context.configure({ device, format, alphaMode: "opaque" });
```

Acquire a presentation texture per frame with `context.getCurrentTexture()`. Reconfigure after device/context replacement; resizing invalidates the current canvas texture.

For GPU simulation, a robust baseline is a sampled source 3D texture plus a write-only storage destination texture:

```wgsl
@group(0) @binding(1) var src : texture_3d<f32>;
@group(0) @binding(2) var dst : texture_storage_3d<rgba32float, write>;
```

Use `textureLoad` for unfiltered source reads and `textureStore` for destination writes. This avoids requiring optional float-filterability. Ping-pong two textures between passes.

WebGL2 is derived from OpenGL ES 3.0 and uses GLSL ES 3.00. It has no general compute-shader stage, so compute-heavy fallback paths require fragment-ping-pong, transform feedback, CPU work, or reduced functionality. Do not pretend WebGL2 has WebGPU-style compute.

## Field abstraction

Normalize model coordinates to a simple bounded domain, usually `[-1,1]^3`:

```text
Field(p,t) -> { s: scalar, v: vec3 }
```

Generic finite-difference operators then become reusable. Keep both the scalar display quantity and the underlying vector-valued operator available: `|grad s|` / `|curl v|` are scalar heatmaps, while `grad s` / `curl v` should drive arrows and direction-aware RGB/HSV encodings:

```text
grad(s) = (s(p+h ex)-s(p-h ex), ...)/(2h)
div(v)  = dvx/dx + dvy/dy + dvz/dz
curl(v) = (dvz/dy-dvy/dz, dvx/dz-dvz/dx, dvy/dx-dvx/dy)
lap(s)  = sum(axis neighbors) - 6s, divided by h²
```

Central differences are a strong default. Choose `h` relative to the field's spatial frequency and the dynamic grid spacing. For sampled simulation data, use a step close to one voxel to avoid differentiating quantization noise.

### Analytic vs numeric derivatives

Use analytic derivatives when they are cheap and obvious, especially for normals of SDFs or potential fields. Use generic finite differences to keep the model API small and to make arbitrary procedural models immediately compatible. For exemplary code, the generic operator is often worth more than a collection of model-specific derivative functions.

## Multiple views, models, and ganged controls

Use one render pipeline and one bind-group layout. Put per-frame values in one uniform buffer and each view in a fixed aligned slot (256-byte offsets are a simple portable choice for dynamic/per-view uniform organization).

A per-view block should minimally carry:

```text
viewport x/y/w/h
model, mode, quantity, colormap
transfer, iso, density, slice
arrow style/density, range min/max
camera delta / local scale
```

Render each view with `setViewport` + `setScissorRect` (WebGPU) or `gl.viewport` + `gl.scissor` (WebGL2). Keep shared controls ganged by writing the same frame values; keep local controls independent by writing only that view's slot.

## 2D scalar/vector visualization

A 2D slice is not a separate model. Sample the 3D field on `p=(x,y,zSlice)`.

Recommended layers:

1. Scalar/derived heatmap.
2. Optional isolines or grid.
3. Procedural vector glyphs.
4. HUD axes and legend.

For dense arrows, procedural fragment glyphs are dramatically cheaper in code than instanced geometry and remain antialiased via smooth distance functions. Quantize screen/world coordinates into cells, evaluate the vector once per cell, rotate the local glyph coordinates into `(direction, perpendicular)`, then evaluate an SDF-like shape.

Useful glyph presets:

- line: direction only, least visual bias;
- chevron: strong orientation cue;
- dart: clear head/tail semantics;
- comet: encode magnitude with glow/length.

For sparse high-quality 3D arrows, switch to instanced geometry and orient a canonical arrow mesh with a basis derived from the vector.

## Scalar/vector color mappings

Keep `colormap(t, vector, preset)` centralized. Support both scalar and directional maps.

Scalar families worth keeping:

- perceptual sequential: Viridis-like;
- hot/high-energy: Inferno/Plasma-like;
- rainbow diagnostic: Turbo-like;
- diverging: Icefire/Spectral-like;
- cyclic: HSV/phase;
- grayscale;
- stylized neon/terrain/ocean variants.

Vector families:

- `abs(normalize(v)) -> RGB` for orientation magnitudes;
- azimuth `atan2(vy,vx)` -> hue, elevation/magnitude -> saturation/value;
- signed component mappings for domain-specific interpretation.

Always make the scalar range explicit. Normalize with `(q-min)/(max-min)` and guard zero-width ranges. When the selected quantity is gradient or curl, route its **vector form** to directional maps/glyphs while routing its magnitude to scalar transfer/range logic. Do not silently color a curl visualization with the unrelated base vector field.

## Isosurface ray rendering

Use a full-screen triangle and reconstruct a ray per fragment. Intersect the ray with the model bounding box first; never march empty space outside the domain.

A compact robust surface marcher for arbitrary fields:

1. Intersect ray with box.
2. Choose the surface field explicitly: base scalar or the selected scalar-valued derived quantity.
3. Step uniformly through `[tNear,tFar]`.
4. Detect a sign change of `surfaceField(p)-iso`.
5. Refine the bracket with 5–8 bisection iterations.
6. Compute the normal from the gradient of the **same surface field**.
7. Shade using the selected display quantity/color map.

Keeping surface-field selection separate from shading quantity is valuable: one view can hold geometry fixed while recoloring by divergence/curl magnitude, while another can expose an actual divergence or Laplacian isosurface. Derived-field marching is much more expensive, so make it opt-in.

This is not sphere tracing unless the field is a true signed-distance function. Do not use distance-sized steps on arbitrary scalar fields.

### Lighting

Use configurable terms:

```text
color = base * (ambient + diffuse * max(dot(n,l),0)) + environmentReflection * specularTerm
```

For the requested sky-sphere specular model, compute the reflected eye direction:

```wgsl
let r = reflect(-viewDir, normal);
let environment = sky(r);
```

For a sky centered on the surface point or camera, intersecting an infinite/unit environment sphere reduces to evaluating the sky function in direction `r`; the directional lookup is the meaningful result. Procedural skies avoid cube-map assets and keep the HTML self-contained.

## Volume rendering

Use front-to-back compositing:

```text
sample = transfer(quantity(p)) -> (rgb, alpha)
contribution = sample.alpha * (1 - accumulated.alpha)
accum.rgb += sample.rgb * contribution
accum.alpha += contribution
```

Early-out once opacity is near one.

A transfer function should own both coloration and opacity. Useful presets include:

- soft emission;
- fire;
- smoke;
- X-ray / low-opacity edge emphasis;
- signed positive/negative lobes;
- glass / narrow iso band;
- aurora / cyclic hue;
- dense plasma.

Expose density as a multiplier rather than baking it into presets. For step-size-independent volume opacity, a production renderer should convert a continuous extinction coefficient using `alpha = 1-exp(-sigma * ds)`; a compact demonstrator may use small bounded per-step alpha but should note the approximation.

## Compute-driven dynamics

Maintain two same-shaped 3D textures. Each compute pass reads A and writes B, then swaps roles. Never read and write the same subresource in an incompatible way inside one pass.

Good demonstrator laws because they have distinct visual behavior and small stencils:

### Gray–Scott reaction diffusion

```text
dA/dt = Da ∇²A - A B² + f(1-A)
dB/dt = Db ∇²B + A B² - (k+f)B
```

### Damped wave

```text
du/dt = v
dv/dt = c² ∇²u - damping*v - restoring*u
```

### Allen–Cahn phase field

```text
du/dt = D ∇²u + u - u³
```

### FitzHugh–Nagumo excitable medium

```text
du/dt = D ∇²u + u - u³/3 - v + I
dv/dt = eps * (u + a - b v)
```

Use periodic wrapping for visually continuous demonstration volumes; use clamped/Neumann/Dirichlet boundaries when the physical model demands them.

Initialize/reset on the GPU with the same compute shader and a reset flag. This avoids CPU creation/upload of a full 3D floating-point volume.

## Performance hierarchy

Optimize in this order:

1. **Empty-space elimination**: box intersection, domain culling, early alpha exit.
2. **Fewer field evaluations**: derivative quantities are expensive; each central difference multiplies model calls.
3. **Lower ray step count / adaptive stepping** where mathematically valid.
4. **Lower dynamic volume resolution** before lowering canvas resolution.
5. **Share pipelines and bind groups**; update buffers rather than recreating GPU objects per frame.
6. **Reduce JavaScript/GPU synchronization**; avoid readback in the animation loop.

For expensive derivative modes, consider precomputing scalar/vector derivatives into additional textures in a compute pass when the same derivatives feed many pixels or views.

## Numerical and visual quality rules

- Gamma-encode only at the final display stage if rendering linear-light values into an unorm canvas target; keep lighting/compositing in linear space.
- Clamp or tone-map large procedural lighting values before encoding.
- Make finite-difference step size a function of model scale or voxel size for production use.
- Use bisection after surface sign changes; it is cheap and stabilizes normals/shading.
- Never normalize a zero vector without an epsilon/fallback.
- Guard reciprocal ray directions and zero ranges.
- Keep volume alpha bounded to preserve numerical stability.
- Distinguish a scalar field from an SDF. A zero-level set does not make a field a distance field.

## WebGPU correctness checklist

- `navigator.gpu` exists and adapter/device creation succeeds.
- Canvas context is `webgpu` and configured with `getPreferredCanvasFormat()`.
- Resource usages include every actual use (`UNIFORM`, `COPY_DST`, `TEXTURE_BINDING`, `STORAGE_BINDING`, etc.).
- Sample type matches the texture format; `rgba32float` sampled without filtering uses `unfilterable-float`.
- Storage texture format/access/view dimension match WGSL.
- Bind-group layout visibility covers every shader stage that accesses a binding.
- Uniform buffer sizes/offsets obey alignment; data layout matches WGSL host-shareable layout.
- Compute dispatch covers the grid and shader bounds-checks global IDs.
- Compute writes finish before the render pass that samples the destination; ordering in one command buffer is sufficient.
- Current canvas texture is acquired for the current frame only.
- Handle `device.lost` and retain a WebGL2 or user-facing fallback path.

## WebGL2 fallback checklist

- Request `webgl2`, not legacy `webgl`.
- Use `#version 300 es` in shaders.
- Use a VAO for draw state, even for `gl_VertexID` full-screen-triangle rendering.
- Remember WebGL fragment coordinates are bottom-left based; reconcile viewport math with CSS/top-left UI coordinates.
- Do not claim compute parity. Either approximate the dynamic model analytically or implement explicit ping-pong fragment/transform-feedback techniques.

## Interaction and UI

A scientific visualizer needs controls that map directly to rendering/math concepts:

- model;
- mode: slice / iso / volume;
- quantity: scalar / vector magnitude / gradient / divergence / curl / Laplacian;
- colormap and transfer function;
- iso value, density, slice coordinate;
- scalar display range;
- glyph style and density;
- camera gang/local mode;
- global ambient/diffuse/specular/shininess and light azimuth/elevation;
- sky preset;
- dynamics law, rate, substeps, reset/pause;
- axes/grid and legend toggles.

Prefer HTML controls and HUD text over implementing typography in shaders. Keep the canvas focused on high-value GPU work.

## Validation workflow

Before handing off a single-file demo:

1. Run JavaScript syntax checking on the extracted script.
2. Open in a WebGPU-capable browser from a secure context (`https://` or a trustworthy localhost origin).
3. Check the console for WGSL compilation or WebGPU validation errors.
4. Force or use a browser without WebGPU to test the WebGL2 fallback.
5. Resize repeatedly and test high-DPI scaling.
6. Exercise every model, quantity, render mode, colormap, transfer function, dynamics law, and camera-ganging state.
7. Profile derivative-heavy volume modes; they can be intentionally expensive.
8. Verify device loss/context loss handling for production deployments.

## Reference implementation in this artifact set

`webgl_webgpu_field_lab.html` demonstrates the architecture above in one dependency-free file:

- four simultaneous independently configured views;
- six model families;
- scalar/vector field visualization and five derivative quantities;
- 3D direction-aware RGB/HSV encoding and slice glyphs that follow the selected base-vector, gradient, or curl field;
- twelve color schemes, including scalar, cyclic, RGB-directional, and HSV-directional mappings;
- eight volume transfer presets;
- four procedural vector glyph styles;
- 3D isosurface ray intersection and bisection refinement, with selectable base-scalar vs derived-quantity surface fields;
- front-to-back volume ray integration;
- configurable ambient/diffuse/specular lighting, shininess, light direction, and procedural sky-sphere reflection;
- WebGPU 3D compute ping-pong with four dynamical laws;
- WebGL2 analytical fallback;
- camera/range ganging, axes/grid and legends.

Treat it as an extensible kernel: add new models in the shared field switch, new derived quantities in the operator layer, and new visual encodings in the centralized color/transfer/glyph functions.
