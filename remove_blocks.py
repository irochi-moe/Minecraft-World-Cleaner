#!/usr/bin/env python3
import csv, gzip, io, os, re, struct, sys, time, zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import nbtlib
import lz4.block
import xxhash

TARGETS = {
    "minecraft:diamond_block",
    "minecraft:gold_block",
    "minecraft:iron_block",
    "minecraft:emerald_block",
    "minecraft:netherite_block",
    "minecraft:chest",
    "minecraft:trapped_chest",
    "minecraft:barrel",
    "minecraft:ancient_debris",
    "minecraft:spawner",
    "minecraft:slime_block",
    "minecraft:ender_chest",
    "minecraft:dispenser",
    "minecraft:dropper",
    "minecraft:hopper",
    "minecraft:shulker_box",
    "minecraft:white_shulker_box",
    "minecraft:orange_shulker_box",
    "minecraft:magenta_shulker_box",
    "minecraft:light_blue_shulker_box",
    "minecraft:yellow_shulker_box",
    "minecraft:lime_shulker_box",
    "minecraft:pink_shulker_box",
    "minecraft:gray_shulker_box",
    "minecraft:light_gray_shulker_box",
    "minecraft:cyan_shulker_box",
    "minecraft:purple_shulker_box",
    "minecraft:blue_shulker_box",
    "minecraft:brown_shulker_box",
    "minecraft:green_shulker_box",
    "minecraft:red_shulker_box",
    "minecraft:black_shulker_box",
}
SIGN_IDS = {"minecraft:sign", "minecraft:hanging_sign"}
EMPTY_MSGS = {"", '""', '{"text":""}', '{"text": ""}'}
PREFILTER = re.compile(b"|".join(re.escape(s.encode()) for s in TARGETS | SIGN_IDS))


def decode_indices(data, palette_len):
    bits = max((palette_len - 1).bit_length(), 4)
    per_long, mask = 64 // bits, (1 << bits) - 1
    if len(data) * per_long < 4096:
        raise ValueError(f"block_states data 부족 (long {len(data)}개)")
    out = []
    for lv in data:
        lv = int(lv) & 0xFFFFFFFFFFFFFFFF
        for _ in range(min(per_long, 4096 - len(out))):
            out.append(lv & mask)
            lv >>= bits
    return out


def encode_indices(indices, palette_len):
    bits = max((palette_len - 1).bit_length(), 4)
    per_long = 64 // bits
    longs = []
    for i in range(0, 4096, per_long):
        v = 0
        for j, idx in enumerate(indices[i:i + per_long]):
            v |= idx << (j * bits)
        longs.append(v - (1 << 64) if v >= 1 << 63 else v)
    return longs


def patch_section(sec, cx, cz, records):
    bs = sec.get("block_states")
    palette = bs.get("palette") if bs else None
    if not palette:
        return False

    targets = {i: str(e.get("Name", "")) for i, e in enumerate(palette)
               if str(e.get("Name", "")) in TARGETS}
    if not targets and len({str(e) for e in palette}) == len(palette):
        return False

    data = bs.get("data")
    if data is None and len(palette) > 1:
        raise ValueError("palette가 여러 개인데 data 없음 (손상 섹션)")
    indices = decode_indices(data, len(palette)) if data is not None else [0] * 4096

    if targets:
        bx, by, bz = cx * 16, int(sec.get("Y", 0)) * 16, cz * 16
        records += [(bx + (p & 15), by + (p >> 8), bz + (p >> 4 & 15), targets[i], "제거")
                    for p, i in enumerate(indices) if i in targets]

    air = nbtlib.Compound({"Name": nbtlib.String("minecraft:air")})
    new_palette, remap, old_to_new = [], {}, []
    for i, entry in enumerate(palette):
        if i in targets:
            entry = air
        key = str(entry)
        if key not in remap:
            remap[key] = len(new_palette)
            new_palette.append(entry)
        old_to_new.append(remap[key])

    bs["palette"] = nbtlib.List[nbtlib.Compound](new_palette)
    if len(new_palette) == 1:
        bs.pop("data", None)
    else:
        bs["data"] = nbtlib.LongArray(
            encode_indices([old_to_new[i] for i in indices], len(new_palette)))
    return True


def empty_msg(m):
    if isinstance(m, dict):
        return set(m) <= {"text"} and str(m.get("text", "")) == ""
    return str(m) in EMPTY_MSGS


def clear_sign_text(be):
    changed = False
    for side in ("front_text", "back_text"):
        msgs = be.get(side, {}).get("messages")
        if not msgs or all(empty_msg(m) for m in msgs):
            continue
        blank = (nbtlib.Compound({"text": nbtlib.String("")})
                 if isinstance(msgs[0], dict) else nbtlib.String('""'))
        be[side]["messages"] = nbtlib.List([blank] * len(msgs))
        changed = True
    return changed


def patch_chunk(root, records):
    sections = root.get("sections")
    if sections is None:
        return False

    cx, cz = int(root.get("xPos", 0)), int(root.get("zPos", 0))
    start = len(records)
    changed = False
    for sec in sections:
        changed |= patch_section(sec, cx, cz, records)

    bes = root.get("block_entities")
    if not bes:
        return changed

    removed = {r[:3] for r in records[start:]}
    if removed:
        keep = [be for be in bes if (int(be.get("x", 0)), int(be.get("y", 0)),
                                     int(be.get("z", 0))) not in removed]
        if len(keep) != len(bes):
            root["block_entities"] = bes = nbtlib.List[nbtlib.Compound](keep)
            changed = True

    for be in bes:
        if str(be.get("id", "")) in SIGN_IDS and clear_sign_text(be):
            records.append((int(be.get("x", 0)), int(be.get("y", 0)),
                            int(be.get("z", 0)), "sign", "내용삭제"))
            changed = True
    return changed


def lz4_compress(data):
    out = bytearray()
    for i in range(0, len(data), 65536):
        block = data[i:i + 65536]
        comp = lz4.block.compress(block, store_size=False)
        token, body = (0x26, comp) if len(comp) < len(block) else (0x16, block)
        out += b"LZ4Block" + struct.pack(
            "<BiiI", token, len(body), len(block),
            xxhash.xxh32_intdigest(block, 0x9747B28C)) + body
    return bytes(out + b"LZ4Block" + struct.pack("<BiiI", 0x16, 0, 0, 0))


def lz4_decompress(payload):
    out, pos = bytearray(), 0
    while True:
        token = payload[pos + 8]
        clen, dlen = struct.unpack_from("<ii", payload, pos + 9)
        pos += 21
        if dlen == 0:
            return bytes(out)
        if clen <= 0:
            raise ValueError(f"LZ4 블록 길이 손상 (clen={clen})")
        block = payload[pos:pos + clen]
        pos += clen
        out += block if token & 0xF0 == 0x10 else \
            lz4.block.decompress(block, uncompressed_size=dlen)


def decompress_chunk(comp_type, payload):
    if comp_type == 2:
        return zlib.decompress(payload)
    if comp_type == 4:
        return lz4_decompress(payload)
    if comp_type == 1:
        return gzip.decompress(payload)
    if comp_type == 3:
        return payload


def process_region(path, dim, full=False):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 8192:
        return 0, 0, []

    fname = os.path.basename(path)
    timestamps = bytearray(data[4096:8192])
    now = struct.pack(">I", int(time.time()))
    chunks, all_records = [], []
    modified = converted = 0

    for i in range(1024):
        entry = data[i * 4:i * 4 + 4]
        offset = int.from_bytes(entry[:3], "big") * 4096
        if not offset or not entry[3]:
            continue

        sector = data[offset:offset + entry[3] * 4096]
        length = struct.unpack(">I", sector[:4])[0] if len(sector) >= 5 else 0
        if not 1 <= length <= len(data) - offset - 4:
            if sector:
                print(f"  [경고] {fname} 청크 {i}: 손상, 원본 보존")
                chunks.append((i, sector))
            continue

        raw = data[offset:offset + 4 + length]
        out = raw
        try:
            nbt = decompress_chunk(raw[4], raw[5:])
            if nbt is not None:
                records, patched = [], False
                if full or PREFILTER.search(nbt):
                    root = nbtlib.File.parse(io.BytesIO(nbt))
                    if patch_chunk(root, records):
                        buf = io.BytesIO()
                        root.write(buf)
                        nbt, patched = buf.getvalue(), True
                if patched or raw[4] != 4:
                    payload, comp = lz4_compress(nbt), b"\x04"
                    if len(payload) + 5 > 255 * 4096:
                        payload, comp = zlib.compress(nbt), b"\x02"
                    out = struct.pack(">I", len(payload) + 1) + comp + payload
                    timestamps[i * 4:i * 4 + 4] = now
                    if patched:
                        modified += 1
                        all_records += [(dim, *r) for r in records]
                    else:
                        converted += 1
        except Exception as e:
            print(f"  [경고] {fname} 청크 {i} 실패, 원본 유지: {e}")
        chunks.append((i, out))

    if not modified and not converted:
        return 0, 0, []

    locations, body, pos = bytearray(4096), bytearray(), 2
    for i, rec in chunks:
        sectors = (len(rec) + 4095) // 4096
        locations[i * 4:i * 4 + 4] = pos.to_bytes(3, "big") + bytes([sectors])
        body += rec + b"\x00" * (sectors * 4096 - len(rec))
        pos += sectors

    with open(path + ".tmp", "wb") as f:
        f.write(locations + timestamps + body)
    os.replace(path + ".tmp", path)
    return modified, converted, all_records


def main():
    repair = "--repair" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--repair"]
    if len(args) != 1:
        sys.exit(f"사용법: python {os.path.basename(__file__)} <월드폴더> [--repair]\n"
                 "  --repair: 모든 청크를 파싱해 손상된 팔레트까지 정규화 (느림)")
    world = args[0]
    if not os.path.isfile(os.path.join(world, "level.dat")):
        sys.exit("level.dat이 없습니다. 올바른 월드 폴더인지 확인하세요.")

    dirs = [(dim if sub == "region" else f"{dim}/{sub}", os.path.join(world, base, sub))
            for dim, base in [("overworld", ""), ("nether", "DIM-1"), ("end", "DIM1")]
            for sub in ("region", "entities", "poi")]

    total_chunks, total_converted, total_blocks = 0, 0, Counter()
    csv_path = os.path.abspath("removed_blocks.csv")

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as cf:
        writer = csv.writer(cf)
        writer.writerow(["dimension", "x", "y", "z", "block", "action"])

        for dim, rdir in dirs:
            if not os.path.isdir(rdir):
                continue
            files = sorted(f for f in os.listdir(rdir) if f.endswith(".mca"))
            print(f"\n[{dim}] 리전 파일 {len(files)}개 처리 중...")

            converted = 0
            with ProcessPoolExecutor() as pool:
                futures = {pool.submit(process_region, os.path.join(rdir, f), dim,
                                       repair): f for f in files}
                for done, fut in enumerate(as_completed(futures), 1):
                    try:
                        n, conv, records = fut.result()
                    except Exception as e:
                        print(f"  [경고] {futures[fut]} 실패, 원본 유지: {e}")
                        continue
                    converted += conv
                    if done % 100 == 0:
                        print(f"  ...{done}/{len(files)}")
                    if not n:
                        continue
                    total_chunks += n
                    counts = Counter()
                    for row in records:
                        name, act = row[4], row[5]
                        counts[name if act == "제거" else f"{name}({act})"] += 1
                        writer.writerow(row)
                    total_blocks += counts
                    detail = ", ".join(f"{k.split(':')[-1]} {v:,}"
                                       for k, v in sorted(counts.items()))
                    print(f"  {futures[fut]}: 청크 {n}개 ({detail or '팔레트 정규화'})")
            print(f"  완료: LZ4 재압축 {converted:,}개 청크")
            total_converted += converted

    print(f"\n완료: 블록 수정 {total_chunks}개, LZ4 재압축 {total_converted:,}개 청크")
    if total_blocks:
        print("처리된 블록:")
        for name, cnt in sorted(total_blocks.items()):
            print(f"  {name}: {cnt:,}개")
        print(f"  합계: {sum(total_blocks.values()):,}개")
        print(f"좌표 목록: {csv_path}")


if __name__ == "__main__":
    main()
