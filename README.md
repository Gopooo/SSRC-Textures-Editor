# SSCR Textures Editor

A community modding tool for **Skylanders Superchargers Racing** that makes texture modding easier.

The tool provides a simple graphical interface for finding, previewing, exporting, and replacing textures stored inside the game's `.pkz` files.

## Features

- Automatically scan a `.pkz` file and list every texture it contains, with name, resolution, format, and mip count.
- Decode and preview CMPR, CMPR_A, RGB5A3, IA8, and RGBA8 textures directly in the app.
- Export any texture to a `.png` at full resolution for editing in any image editor.
- Re-import an edited `.png` and write it back into the texture (CMPR and CMPR_A formats).
- Apply edits to multiple textures within the same `.pkz` before saving.
- Search/filter the texture list by name.
- Save a brand-new `.pkz` containing all your edits, without modifying the original file.

## Requirements

- **Python 3.8 or newer**
- **Tkinter** for the graphical interface
- **Pillow** and **numpy** (see below)

Tkinter normally ships with the official Windows installer from python.org.

Install the required third-party packages:

```bat
pip install Pillow numpy --break-system-packages
```

(The `--break-system-packages` flag is only needed on some Linux distributions that lock down the system Python; on Windows you can usually just run `pip install Pillow numpy`.)

## Preparing the Game Files

Before using the tool, you need to extract the game files from your **Skylanders SuperChargers Racing Wii ISO**.

A recommended method is to use **Wiimms ISO Tools (WIT)** to extract the ISO contents.

After extraction, locate the game's `Data` folder.

The tool does **not** extract the Wii ISO itself. It expects the game files to already be extracted.

## Usage

Run:

    python sscr_textures_editor.py

### Editing a texture

1. Click **Open .pkz** and select a `.pkz` file from the extracted `Data` folder.
2. The texture list on the left fills in automatically. Each entry shows:
   - `[OK]` — a texture that can be both previewed and edited (CMPR / CMPR_A)
   - `[RO]` — a texture that can be previewed but not edited yet (RGBA8, IA8, RGB5A3)
   - `[??]` — an unrecognized format
3. Use the **Search** box to filter the list by name.
4. Click a texture to see a live preview on the right.
5. Click **Export image (PNG)...** to save it to disk at full resolution.
6. Edit the PNG in your image editor of choice, keeping the exact same pixel resolution.
7. Click **Load custom image...** and select your edited PNG (this only updates the in-app preview so far).
8. Click **Apply to this texture** to write the change into memory. The entry is marked `*EDITED*` in the list.
9. Repeat steps 4–8 for as many textures as you want to change in this file.
10. Once you're done, click **Save modified .pkz...** to write out a new `.pkz` containing every edit.

## Technical notes (binary format)

Everything below was reverse-engineered by comparing many `.pkz` files. Documented here for anyone who wants to extend this tool. All values are **big-endian**, as is standard for GameCube/Wii hardware.

Texture chunk hierarchy:

```text
0x0009
└── 0x138D
    ├── 0x138E   ResourceHeader (Unique ID + Name)
    └── 0x0191
        └── 0x0197   TextureInfo
```

`TextureInfo` (type `0x0197`, version `4`, fixed length `0x30`) payload layout:

| Offset | Size | Field                                       |
|--------|------|-----------------------------------------------|
| 0x00   | 4    | **height** (note: height comes before width!) |
| 0x04   | 4    | width                                          |
| 0x08   | 4    | mip level count                                |
| 0x0C   | 4    | format code                                    |
| 0x10   | 4    | color data length (CMPR chain only)            |

The raw pixel payload lives separately, in a type-4 `0x0026` asset's `0x0195` child. It's linked back to its `TextureInfo` entry by matching the `ResourceHeader`'s Unique ID — this hash-based matching is the only reliable way to pair metadata with pixel data.

**Format codes:** `0`=RGBA8, `1`=IA8, `2`=RGB5A3, `3`=CMPR, `4`=CMPR_A, `5`=CMPR.

CMPR uses 8x8 "super blocks" made of four 4x4 DXT1 sub-blocks (TL, TR, BL, BR order). CMPR_A textures store a full CMPR color chain followed by a full I8 (8-bit grayscale) alpha chain, used as the alpha channel.

### Known limitations

- Only CMPR and CMPR_A can currently be edited. RGBA8 / IA8 / RGB5A3 are preview-only for now.
- Editing only replaces the base (largest) mip level; lower mip levels are left as-is.
- The CMPR encoder used for saving is a simple min/max-endpoint compressor — good enough for modding, but not as refined as a dedicated DXT compressor.
- Texture resolution can't be changed; the replacement image must match the original's pixel dimensions.

## Credits / Special Thanks

This project would have been much harder to develop without the existing work and research of the Skylanders modding community.

Special thanks to **maff** from the **Skylanders Reverse Engineering Discord server**. His **Cogwheel** tool was extremely helpful for understanding and inspecting the game's PKZ resource structure, including chunks, resource data, and the way game assets are stored.

Thanks to everyone in the Skylanders reverse-engineering and modding communities who has documented the game formats, experimented with the files, and shared their findings.

## Disclaimer

This is an unofficial community-made modding tool and is not affiliated with or endorsed by Activision, Toys for Bob, Beenox, or Nintendo.

Always keep backups of the original game files before modifying anything.

## Rebuilding the Modified Wii ISO

After you finish replacing textures and have copied the modified `.pkz` file(s) back into the extracted game files, you need to rebuild the Wii ISO before testing it.

You can use **Wiimms ISO Tools (WIT)** again to rebuild the extracted game directory into a new ISO.

## License

This project's source code is licensed under the [MIT License](LICENSE).

This license covers the tool itself only — it does **not** grant any rights to the game or its assets (textures, models, audio, etc.), which remain the property of their respective owners. No copyrighted game content is included or redistributed with this tool.
