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
import os
from glob import glob
from os.path import abspath
from os.path import basename
import subprocess
import uuid

GPT_ROOT_X86_64 = uuid.UUID('4f68bce3-e8cd-4db1-96e7-fbcaf984b709')
GPT_BIOS = uuid.UUID('21686148-6449-6e6f-744e-656564454649')

SECTOR = 512
MB = 1024 * 1024
GB = 1024 * 1024 * 1024
FOOTER_SECTORS = 34

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

ENA_UNIT = """[Match]
Driver=ena

[Network]
DHCP=yes
"""

def roundupMiB(x: int) -> int:
    return (x + 1048575) & ~1048575


def inlay_disk(root_uuid, boot_uuid, filename, outfile):
    """Embed the squashfs into a GPT disklabel.

    Ideally we'd use EFI to boot kernel images directly, skipping the bootloader
    entirely. So we'd use something like mkosi's gpt_squashfs output format and have
    a small vfat partition to contain it all. EC2 still uses BIOS boot, so we need a
    traditional bootloader.

    Can the bootloader be packaged into the squashfs somehow? Probably. For grub, it 
    would look like:
     * boot sector contains boot.img
     * BIOS boot partition contains core.img, which knows how to find+read squashfs
     * squashfs contains kernel and initrd, which boots into userspace

    What we have instead is:
     * boot sector contains boot.img
     * BIOS "boot" partition contains core.img, which knows how to find+read ext4
     * ext4 /boot contains kernel and initrd
     * squashfs contains userspace
    """

    blob_sectors = roundupMiB(os.stat(filename).st_size) // SECTOR

    with open(outfile, "wb") as out_fh:
        out_fh.truncate(GB)
        remaining = GB // SECTOR - blob_sectors - 4096 - FOOTER_SECTORS

        table = [
            "label: gpt",
            "first-lba: 2048",
            'size=2048, type=21686148-6449-6e6f-744e-656564454649, name="BIOS boot partition"',
            'size={}, uuid={}, type=4f68bce3-e8cd-4db1-96e7-fbcaf984b709, attrs=GUID:60, name="Root Partition"'.format(blob_sectors, root_uuid),
            'size={}, uuid={}, type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="Boot Partition"'.format(remaining, boot_uuid),
        ]

        run(["sfdisk", "--color=never", outfile], input='\n'.join(table).encode('utf-8'))
        out_fh.seek(4096 * SECTOR)
        with open(filename, "rb") as blob:
            while True:
                data = blob.read(SECTOR)
                if len(data) == 0:
                    break

                out_fh.write(data)


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


def set_up_boot(raw_image, boot_partuuid):
    boot_dir = abspath("image/boot")

    with attach_image_loopback(raw_image) as loopdev:
        run(["mkfs.ext4", f"{loopdev}p3"])

        script = f"""
        #!/bin/bash
        set -o errexit
        set -o nounset
        set -o pipefail
        mount /dev/disk/by-partuuid/{boot_partuuid} /boot
        cp -a /mnt/* /boot
        grub-install {loopdev}
        """.encode("utf-8")


        run([
            "systemd-nspawn",
            "-i", loopdev,
            f"--bind-ro={loopdev}",
            f"--bind-ro={loopdev}p1",
            f"--bind-ro={loopdev}p2",
            f"--bind={loopdev}p3",
            f"--bind-ro={boot_dir}:/mnt",
            f"--bind-ro=/dev/block",
            f"--bind-ro=/dev/disk",
            f"--property=DeviceAllow={loopdev}",
            "--pipe",
        ], input=script)


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
    Path("image/etc/sysctl.d/50-dad.conf").write_text("net.ipv6.conf.ens5.accept_dad = 0\n")
    Path("image/etc/systemd/network/ena.network").write_text(ENA_UNIT)
    Path("image/etc/systemd/system/multi-user.target.wants/systemd-networkd.service").symlink_to("/lib/systemd/system/systemd-networkd.service")
    Path("image/root/.ssh").mkdir()
    Path("image/root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKr4DFWVEoLCTgjtzl3wT+JnYnDojJAS/4hsFww4n/R8 josh@ubuntu\n")

    for key in glob("image/etc/ssh/ssh_host_*_key"):
        filename = basename(key)
        os.remove(key)
        os.symlink("/var/lib/ssh/" + filename, key)

    for key in glob("image/etc/ssh/ssh_host_*_key.pub"):
        os.remove(key)

    Path("image/etc/fstab").write_text("none /tmp tmpfs defaults 0 0\n")

    overlay_script_path = Path("image/usr/local/sbin/mount-overlay")
    overlay_script_path.write_text(OVERLAY_SCRIPT)
    overlay_script_path.chmod(0o755)

    overlay_unit_path = Path("image/etc/systemd/system/overlays.service")
    overlay_unit_path.write_text(OVERLAY_UNIT)

    units = "image/etc/systemd/system/"
    Path(f"{units}/ssh-keygen.service").write_text(SSH_KEYGEN_UNIT)
    Path(f"{units}/ssh.service.requires").mkdir()
    Path(f"{units}/ssh.service.requires/ssh-keygen.service").symlink_to("/etc/systemd/system/ssh-keygen.service")

    Path("image/etc/systemd/system/sysinit.target.wants/overlays.service").symlink_to(overlay_unit_path)

    mask_service("grub-initrd-fallback")
    mask_service("cloud-init-local")


def main():
    run([
        "mkosi",
        "--repositories", "main,universe",
        "-t", "directory",
        "-p grub-pc",
        "-p linux-image-aws",
        "-p initramfs-tools",
        "-p cloud-init",
        "-p openssh-server",
        "-p lsb-release",
        "-p less",
        "-p nginx-light",
        "-p tcpdump",
        "--debug", "run",
    ])
    change_passwords("image")
    root_partuuid = str(uuid.uuid4())
    boot_partuuid = str(uuid.uuid4())
    outfile = "image.raw"
    squashfs_image = "image.squashfs"

    # The initramfs will automatically pick a swap device on the builder system to 
    # resume from. Of course this doesn't work on a different machine, so we do
    # noresume
    Path("image/boot/grub/grub.cfg").write_text(
        dedent(f"""\
        echo 'GRUB booting'
        linux /vmlinuz root=PARTUUID={root_partuuid} ro console=tty0 console=ttyS0,115200n8 noresume
        initrd /initrd.img
        boot
        """
        )
    )

    set_up_overlay()
    rm_f(squashfs_image)
    run([
        "mksquashfs", "image", squashfs_image,
        "-comp", "zstd", "-processors", "1",
        "-wildcards",
        "-e", "boot/*"
    ])
    inlay_disk(root_partuuid, boot_partuuid, squashfs_image, outfile)
    set_up_boot(outfile, boot_partuuid)
    compress_product(outfile)


if __name__ == '__main__':
    main()
