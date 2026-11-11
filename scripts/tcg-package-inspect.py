"""Extract and inspect staged TCG packages; never execute assemblies or install them."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile

import dnfile


def compressed(data, position):
    first = data[position]
    if first < 0x80:
        return first, position + 1
    if first < 0xC0:
        return ((first & 0x3F) << 8) | data[position + 1], position + 2
    return ((first & 0x1F) << 24) | int.from_bytes(data[position + 1:position + 4], "big"), position + 4


def text(data, position):
    if data[position] == 0xFF:
        return None, position + 1
    length, position = compressed(data, position)
    return data[position:position + length].decode("utf-8"), position + length


def attributes(pe):
    output = []
    table = pe.net.mdtables.CustomAttribute
    for attribute in table.rows if table else []:
        constructor = attribute.Type.row
        owner = getattr(getattr(constructor, "Class", None), "row", None)
        kind = str(getattr(owner, "TypeName", ""))
        if kind not in ("BepInPlugin", "BepInDependency", "BepInIncompatibility", "TargetFrameworkAttribute"):
            continue
        data = attribute.Value.value
        if data[:2] != b"\x01\x00":
            raise ValueError("Invalid attribute encoding")
        value, position = text(data, 2)
        item = {"kind": kind, "value": value}
        if kind == "BepInPlugin":
            item["name"], position = text(data, position)
            item["version"], position = text(data, position)
        elif kind == "BepInDependency":
            if constructor.Signature.value[-1] == 0x0E:
                item["minimum_version"], position = text(data, position)
                item["flags"] = 1
            else:
                item["flags"] = int.from_bytes(data[position:position + 4], "little")
        output.append(item)
    return output


def inspect(path):
    pe = dnfile.dnPE(str(path))
    if pe.net is None:
        raise ValueError(f"Expected managed DLL: {path.name}")
    assembly = pe.net.mdtables.Assembly.rows[0]
    references = pe.net.mdtables.AssemblyRef
    result = {
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "assembly": str(assembly.Name), "attributes": attributes(pe),
        "references": [str(row.Name) for row in references.rows] if references else [],
    }
    pe.close()
    return result


def main():
    root = Path(sys.argv[1]).resolve()
    packages = []
    for archive_path in sorted((root / "archives").glob("*.zip")):
        identifier = archive_path.stem
        destination = root / "expanded" / identifier
        entries = []
        with zipfile.ZipFile(archive_path) as archive:
            members = []
            for member in archive.infolist():
                if member.filename.replace("\\", "/").endswith("/"):
                    if member.file_size or stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError(f"Invalid directory entry in {identifier}")
                    continue
                members.append(member)
            if sum(member.file_size for member in members) > 512 * 1024 * 1024:
                raise ValueError("Uncompressed package exceeds audit limit")
            seen = set()
            for member in members:
                name = member.filename.replace("\\", "/")
                parts = name.split("/")
                if name.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in parts):
                    raise ValueError(f"Unsafe archive path in {identifier}")
                if stat.S_ISLNK(member.external_attr >> 16) or name.casefold() in seen:
                    raise ValueError(f"Symlink or duplicate archive path in {identifier}")
                seen.add(name.casefold())
                data = archive.read(member)
                path = destination.joinpath(*PurePosixPath(name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    if path.read_bytes() != data:
                        raise ValueError(f"Existing staged file differs: {path}")
                else:
                    with path.open("xb") as stream:
                        stream.write(data)
                entries.append({"relative_path": name, "bytes": len(data),
                                "sha256": hashlib.sha256(data).hexdigest()})
        assemblies = [inspect(destination / entry["relative_path"])
                      for entry in entries if entry["relative_path"].lower().endswith(".dll")]
        packages.append({"id": identifier, "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                         "files": entries, "assemblies": assemblies})
        print(identifier)
        for assembly in assemblies:
            print(" ", assembly["assembly"], json.dumps(assembly["attributes"], ensure_ascii=False))
    report = root / "package-inspection.json"
    with report.open("w", encoding="utf-8") as stream:
        json.dump(packages, stream, ensure_ascii=False, indent=2)
    print("Inspection saved:", report)


if __name__ == "__main__":
    main()
