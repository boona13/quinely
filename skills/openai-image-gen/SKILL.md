---
name: openai-image-gen
description: Generate images using Quinely's native AI image generation tool
triggers:
  - generate image
  - create image
  - make image
  - draw
  - image generation
  - make a picture
  - create a visual
  - design graphic
  - generate art
  - ai image
  - generate photo
  - make a graphic
  - text to image
  - create thumbnail
  - make poster
tools:
  - generate_image
priority: 5
---

# Image Generation

Use Quinely’s native `generate_image` tool for image generation requests.

## Usage

```python
generate_image(
  prompt='Detailed image description',
  style='photorealistic',
  size='landscape'
)
```

### Parameters

- **prompt** (required): Describe subject, composition, lighting, colors, and mood.
- **style** (optional): e.g. `photorealistic`, `illustration`, `digital art`, `watercolor`, `minimalist`, `3d render`, `pixel art`.
- **size** (optional): `landscape` (16:9), `portrait` (9:16), or `square` (1:1).
- **filename** (optional): Output filename override.

### Output

The tool saves a PNG image in `~/.ghost/generated_images/` and returns metadata such as:
- `path`
- `filename`
- `size_kb`

## Prompt Tips

1. Be specific about the main subject.
2. Add composition details (close-up, wide shot, centered subject, etc.).
3. Specify lighting and mood.
4. Include color palette/style direction.
5. Mention intended use (thumbnail, banner, social post) when relevant.

## Example

```python
generate_image(
  prompt='Modern tech podcast cover with a neon purple/blue gradient, abstract waveform lines, clean center area for title text',
  style='digital art',
  size='square'
)
```
