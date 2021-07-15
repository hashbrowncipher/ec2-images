"""
Dependencies:
  mkosi
  debootstrap
  mksquashfs (squashfs-tools)
  systemd-nspawn (systemd-container)
  lz4 (liblz4-tool)
"""

from contextlib import contextmanager
from subprocess import PIPE
from pathlib import Path
from textwrap import dedent
from textwrap import dedent as dd
from tempfile import TemporaryDirectory
import os
from os import chdir
from os import makedirs
from glob import glob
from os.path import abspath
from os.path import basename
from os.path import exists
from shutil import copyfile
from shutil import rmtree
import subprocess
import uuid

GPT_ROOT_X86_64 = uuid.UUID('4f68bce3-e8cd-4db1-96e7-fbcaf984b709')
GPT_BIOS = uuid.UUID('21686148-6449-6e6f-744e-656564454649')

SECTOR = 512
MB = 1024 * 1024
GB = 1024 * 1024 * 1024
FOOTER_SECTORS = 34
ESP_SECTORS = 409600

OVERLAY_SCRIPT = """#!/bin/bash
set -o errexit
set -o nounset
set -o pipefail

mkdir /run/overlay
cd /run/overlay
mkdir var var.work
mount -t overlay -o lowerdir=/var,upperdir=var,workdir=var.work none /var
"""

OVERLAY_UNIT = """[Unit]
Description=Mount overlay fses
DefaultDependencies=no
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mount-overlay
RemainAfterExit=yes
"""

SSH_KEYGEN_UNIT = """[Unit]
Description=Create SSH host key
Before=ssh.service
ConditionPathExists=!/var/lib/ssh/ssh_host_ed25519

[Service]
Type=oneshot
ExecStart=mkdir /var/lib/ssh
ExecStart=ssh-keygen -q -f /var/lib/ssh/ssh_host_ed25519_key -N '' -t ed25519
RemainAfterExit=yes
"""

# WithoutRA speeds up the address acquisition process by not waiting for an inbound RA
# packet before performing DHCP.
ENA_UNIT = """[Match]
Driver=ena

[Network]
DHCP=yes

[DHCPv4]
UseHostname=no

[DHCPv6]
WithoutRA=solicit
"""

HOSTNAME_UNIT = """[Unit]
After=cloud-config.target
Wants=cloud-config.target

[Service]
ExecStart=/bin/bash -c 'hostname $(cloud-init query v1.instance_id)'
Type=oneshot
RemainAfterExit=yes
"""

CLOUD_CFG = """\
network:
  config: disabled

cloud_init_modules:
 - bootcmd
 - write-files
 - growpart
 - resizefs
 - disk_setup
 - mounts
 - rsyslog

cloud_config_modules:
# Emit the cloud config ready event
# this can be used by upstart jobs for 'start on cloud-config'.
 - runcmd
"""

def roundupMiB(x: int) -> int:
    return (x + 1048575) & ~1048575


def copy(in_fh, out_fh):
    while True:
        data = in_fh.read(SECTOR)
        if len(data) == 0:
            break

        out_fh.write(data)


def format_disk(esp_uuid, root_partuuid, outfile):
    with open(outfile, "wb") as out_fh:
        out_fh.truncate(GB)

    remaining = GB // SECTOR - (2048 + ESP_SECTORS + FOOTER_SECTORS)
    table = [
        "label: gpt",
        "first-lba: 2048",
        f'size={ESP_SECTORS}, uuid={esp_uuid}, type=c12a7328-f81f-11d2-ba4b-00a0c93ec93b, name="EFI System Partition"',
        f'size={remaining}, uuid={root_partuuid}, type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="State Partition"',
    ]

    run(["sfdisk", "--color=never", outfile], input='\n'.join(table).encode('utf-8'))


def inlay_disk(root_partuuid, esp_uuid, filename, outfile):
    """Embed the squashfs into a GPT disklabel.

    Ideally we'd use EFI to boot kernel images directly, skipping the bootloader
    entirely. So we'd use something like mkosi's gpt_squashfs output format and have
    a small vfat partition to contain it all. EC2 still uses BIOS boot, so we need a
    traditional bootloader.
    """




@contextmanager
def attach_image_loopback(filename):
    c = run(["losetup", "--find", "--show", "--partscan", filename], stdout=PIPE)
    loopdev = c.stdout.decode("utf-8").strip()

    try:
        yield loopdev
    finally:
        run(["losetup", "--detach", loopdev])


def run(*args, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run(*args, **kwargs)

@contextmanager
def mountedcwd(device):
    with TemporaryDirectory(dir=".") as mountpoint:
        run(["mount", device, mountpoint])
        chdir(mountpoint)
        try:
            yield
        finally:
            chdir("..")
            run(["umount", mountpoint])


def set_up_boot(raw_image, root_partuuid, esp_uuid):
    boot_dir = abspath("image/boot")

    with attach_image_loopback(raw_image) as loopdev:
        run(["mkfs.vfat", f"{loopdev}p1"])
        with mountedcwd(f"{loopdev}p1"):
            makedirs("loader/entries")
            Path("loader/entries/ubuntu.conf").write_text(dd(f"""\
            title   Ubuntu 21.04
            linux   /vmlinuz
            initrd  /initrd.img
            options root=PARTUUID={root_partuuid} loop=root.squashfs console=ttyS0 quiet
            """))

            makedirs("EFI/boot")
            copyfile("/usr/lib/systemd/boot/efi/systemd-bootx64.efi", "EFI/boot/bootx64.efi")

            for name in glob("../boot/*"):
                copyfile(name, basename(name))
                os.remove(name)

        run(["mkfs.ext4", f"{loopdev}p2"])
        with mountedcwd(f"{loopdev}p2"):
            copyfile("../image.squashfs", "root.squashfs")


def rm_f(filename):
    try:
        os.remove(filename)
    except FileNotFoundError:
        pass


def compress_product(outfile):
    """Compress the final product.

    Since the expectation is that this will get uploaded to S3, which doesn't have
    any mechanism for sparse encoding or transfer, we'll just use LZ4 to wring the zero
    regions out of the finished file."""
    rm_f(f"{outfile}.lz4")
    run(["lz4", outfile])


def change_passwords(image):
    new_lines = []
    with open(image + "/etc/shadow", "r+") as shadow:
        for entry in shadow:
            fields = entry.split(":")
            if fields[0] == "root":
                fields[1] = ""

            new_lines.append(':'.join(fields))

        shadow.seek(0)
        shadow.truncate()
        for line in new_lines:
            shadow.write(line)


def mask_service(name):
    Path(f"image/etc/systemd/system/{name}.service").symlink_to("/dev/null")


def set_up_overlay():
    Path("image/etc/systemd/network/ena.network").write_text(ENA_UNIT)
    Path("image/etc/cloud/cloud.cfg.d/50_custom.cfg").write_text(CLOUD_CFG)
    Path("image/etc/systemd/system/multi-user.target.wants/systemd-networkd.service").symlink_to("/lib/systemd/system/systemd-networkd.service")
    Path("image/efi").mkdir()
    Path("image/root/.ssh").mkdir()
    Path("image/root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKr4DFWVEoLCTgjtzl3wT+JnYnDojJAS/4hsFww4n/R8 josh@ubuntu\n")

    for key in glob("image/etc/ssh/ssh_host_*_key"):
        filename = basename(key)
        os.remove(key)
        os.symlink("/var/lib/ssh/" + filename, key)

    for key in glob("image/etc/ssh/ssh_host_*_key.pub"):
        os.remove(key)

    Path("image/etc/fstab").write_text("none /tmp tmpfs defaults 0 0\n")
    Path("image/etc/initramfs-tools/initramfs.conf").write_text("MODULES=dep\nCOMPRESS=lz4\n")

    overlay_script_path = Path("image/usr/local/sbin/mount-overlay")
    overlay_script_path.write_text(OVERLAY_SCRIPT)
    overlay_script_path.chmod(0o755)

    units = "image/etc/systemd/system/"

    overlay_unit_path = Path(f"{units}/overlays.service")
    overlay_unit_path.write_text(OVERLAY_UNIT)
    Path(f"{units}/sysinit.target.wants/overlays.service").symlink_to("/etc/systemd/system/overlays.service")

    hostname_unit_path = Path(f"{units}/hostname.service")
    hostname_unit_path.write_text(HOSTNAME_UNIT)
    Path(f"{units}/multi-user.target.wants/hostname.service").symlink_to("/etc/systemd/system/hostname.service")

    Path(f"{units}/ssh-keygen.service").write_text(SSH_KEYGEN_UNIT)
    Path(f"{units}/ssh.service.requires").mkdir()
    Path(f"{units}/ssh.service.requires/ssh-keygen.service").symlink_to("/etc/systemd/system/ssh-keygen.service")


def extract_kernel():
    run([
        "systemd-nspawn",
        "-D", "image",
        "apt-get", "install", "-y", "--no-install-recommends", "linux-image-aws",
    ])

    if exists("boot"):
        rmtree("boot")
    os.mkdir("boot")
    copyfile("image/boot/vmlinuz", "boot/vmlinuz")
    copyfile("image/boot/initrd.img", "boot/initrd.img")
    rmtree("image/boot")


def main():
    root_partuuid = str(uuid.uuid4())
    esp_uuid = str(uuid.uuid4())
    outfile = "image.raw"
    squashfs_image = "image.squashfs"

    run([
        "mkosi",
        "--repositories", "main,universe",
        "-d", "ubuntu",
        "-r", "hirsute",
        "-t", "directory",
        "-p cloud-init",
        "-p openssh-server",
        "-p lsb-release",
        "-p less",
        "-p curl",
        "-p jq",
        "-p nginx-light",
        "-p tcpdump",
        "-p initramfs-tools",
        "-p python3-pip",
        "-p python3-venv",
        "-p vim-nox",
        "--debug", "run",
    ])
    change_passwords("image")
    extract_kernel()

    set_up_overlay()
    rm_f(squashfs_image)
    run([
        "mksquashfs", "image", squashfs_image,
        "-comp", "zstd", "-processors", "1",
        "-wildcards",
        "-e", "boot/*"
    ])
    format_disk(root_partuuid, esp_uuid, outfile)
    set_up_boot(outfile, root_partuuid, esp_uuid)
    compress_product(outfile)


if __name__ == '__main__':
    main()
