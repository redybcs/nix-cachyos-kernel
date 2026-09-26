import functools
import json
import re
import subprocess
import tempfile
from pathlib import Path

import requests

REPO = "https://github.com/CachyOS/linux-cachyos.git"
PATCHES_REPO = "https://github.com/CachyOS/kernel-patches.git"
ZFS_META_URL = "https://raw.githubusercontent.com/CachyOS/zfs/{commit}/META"
ZFS_ARCHIVE_URL = "https://github.com/CachyOS/zfs/archive/{commit}.tar.gz"


@functools.lru_cache(None)
def nix_sha256_to_sri(hash: str) -> str:
    cmd = ["nix", "hash", "to-sri", "--type", "sha256", hash]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(f"nix hash command failed with return code: {result.returncode}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("nix hash output is empty")

    return output


@functools.lru_cache(None)
def run_nix_prefetch_url(url: str) -> str:
    cmd = ["nix-prefetch-url", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(
            f"nix-prefetch-url failed for {url} with return code {result.returncode}: {result.stderr.strip()}"
        )

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("nix-prefetch-url output is empty")

    return output


@functools.lru_cache(None)
def run_nix_prefetch_git_subfolder(url: str, rev: str, subfolder: str) -> str:
    cmd = [
        "nix-prefetch-git",
        "--url",
        url,
        "--rev",
        rev,
        "--root-dir",
        subfolder,
        "--quiet",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(
            f"nix-prefetch-git command failed with return code: {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("nix-prefetch-git output is empty")

    data = json.loads(output)
    return data["hash"]


def get_srcname(pkgbuild_text: str) -> str:
    script = pkgbuild_text + "\necho $_srcname"
    result = subprocess.run(
        ["bash"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def srcname_to_version(srcname: str) -> str:
    return "-".join(srcname.split("-")[1:-1])


def find_variants(repo_dir: Path) -> list[str]:
    return [
        p.name
        for p in repo_dir.iterdir()
        if p.is_dir()
        and p.name.startswith("linux-cachyos")
        and (p / "PKGBUILD").exists()
        and (p / "config").exists()
    ]


def major_minor(version: str) -> str:
    parts = re.findall(r"[0-9]+|[a-zA-Z]+", version)
    return ".".join(parts[:2])


def get_rev(repo: str | Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def get_zfs_commit(pkgbuild_text: str) -> str:
    commit = re.search(r"zfs\.git#commit=([0-9a-f]{40})", pkgbuild_text)
    if not commit:
        raise ValueError("Cannot find ZFS commit ID in PKGBUILD")
    return commit[1]


@functools.lru_cache(None)
def get_zfs_version(commit: str) -> str:
    url = ZFS_META_URL.format(commit=commit)
    print(f"{url=}")
    metadata = requests.get(url, timeout=300).text
    version = re.search(r"^Version:\s+([0-9\.]+)$", metadata, re.MULTILINE)
    if not version:
        raise ValueError(f"Cannot find ZFS version for {commit=}")
    return version[1]


@functools.lru_cache(None)
def get_zfs_hash(commit: str) -> str:
    url = ZFS_ARCHIVE_URL.format(commit=commit)
    print(f"{url=}")
    return nix_sha256_to_sri(run_nix_prefetch_url(url))


class TemporaryGitRepo(tempfile.TemporaryDirectory):
    def __init__(self, repo_url: str, **kwargs):
        super().__init__(**kwargs)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", repo_url, str(self.name)],
            check=True,
        )

    def __enter__(self) -> Path:
        return Path(self.name)


if __name__ == "__main__":
    with (
        TemporaryGitRepo(REPO, ignore_cleanup_errors=True) as dir,
        TemporaryGitRepo(PATCHES_REPO, ignore_cleanup_errors=True) as patches_dir,
    ):
        commit = get_rev(dir)
        patches_commit = get_rev(patches_dir)
        print(f"{commit=} {patches_commit=}")

        current = Path.cwd()
        while not (current / "flake.lock").exists():
            if current == current.parent:
                raise RuntimeError("Could not find flake.lock in any parent directory, exiting")
            current = current.parent

        kernel_output_file = current / "kernel-cachyos" / "version.json"
        zfs_output_file = current / "zfs-cachyos" / "version.json"
        previous_variants = json.loads(kernel_output_file.read_text())
        previous_zfs_versions = json.loads(zfs_output_file.read_text())
        variants = {}
        zfs_versions = {}

        for variant in find_variants(dir):
            print(f"Variant {variant}")
            pkgbuild = (dir / variant / "PKGBUILD").read_text()
            srcname = get_srcname(pkgbuild)
            version = srcname_to_version(srcname)
            url = f"https://github.com/CachyOS/linux/releases/download/{srcname}/{srcname}.tar.gz"
            try:
                hash = nix_sha256_to_sri(run_nix_prefetch_url(url))
            except RuntimeError as error:
                if variant not in previous_variants or variant not in previous_zfs_versions:
                    raise
                print(f"  skipping {variant}: {error}")
                variants[variant] = previous_variants[variant]
                zfs_versions[variant] = previous_zfs_versions[variant]
                continue
            print(f"  kernel: {srcname=} {version=} {hash=}")

            config_url = (
                f"https://raw.githubusercontent.com/CachyOS/linux-cachyos/{commit}/{variant}/config"
            )
            config_hash = nix_sha256_to_sri(run_nix_prefetch_url(config_url))
            print(f"  config: {config_hash=}")

            patch_version = major_minor(version)
            patch_hash = run_nix_prefetch_git_subfolder(
                PATCHES_REPO, patches_commit, f"{patch_version}/"
            )
            print(f"  patches: {patch_version=} {patches_commit=} {patch_hash=}")

            variants[variant] = {
                "version": version,
                "url": url,
                "hash": hash,
                "configUrl": config_url,
                "configHash": config_hash,
                "patchVersion": patch_version,
                "patchRev": patches_commit,
                "patchHash": patch_hash,
            }

            zfs_commit = get_zfs_commit(pkgbuild)
            zfs_version = get_zfs_version(zfs_commit)
            zfs_hash = get_zfs_hash(zfs_commit)
            zfs_url = ZFS_ARCHIVE_URL.format(commit=zfs_commit)
            print(f"  zfs: {zfs_commit=} {zfs_version=} {zfs_hash=}")
            zfs_versions[variant] = {
                "commit": zfs_commit,
                "version": zfs_version,
                "url": zfs_url,
                "hash": zfs_hash,
            }

    with open(kernel_output_file, "w", encoding="utf-8") as f:
        json.dump(variants, f, indent=2, sort_keys=True)

    with open(zfs_output_file, "w", encoding="utf-8") as f:
        json.dump(zfs_versions, f, indent=2, sort_keys=True)
