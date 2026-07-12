# Minecraft World Cleaner

A Python utility for processing Minecraft Java Edition world region files.

## Features

* Reads region files compressed with **LZ4**, **Zlib**, **Gzip**, or **raw NBT**.
* Converts chunks to **LZ4** compression whenever possible.
* Automatically falls back to **Zlib** if a chunk exceeds the LZ4 size limit.
* Removes selected blocks and their associated block entities.
* Clears text from Signs and Hanging Signs.
* Generates a `removed_blocks.csv` report.
* Supports a `--repair` mode for full chunk normalization.

## Requirements

* Python 3.10+
* `nbtlib`
* `lz4`
* `xxhash`

```bash id="g5d1cr"
pip install nbtlib lz4 xxhash
```

## Usage

```bash id="hzz2wq"
python world_cleaner.py <world_directory>
```

Repair mode:

```bash id="wlk33x"
python world_cleaner.py <world_directory> --repair
```

> **Warning**
>
> This tool modifies world data in place. Always back up your world before use.

## License

MIT License.
