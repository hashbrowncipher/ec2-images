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

IMDS_UNIT = """\
[Unit]
After=network.target
StandardOutput=file:/run/instance-identity

[Service]
Type=oneshot
ExecStart=curl -v --retry 2 169.254.169.254/latest/dynamic/instance-identity/document
RemainAfterExit=yes
"""

HOSTNAME_UNIT = """\
[Unit]
Requires=imds.service

[Service]
Type=oneshot
ExecStart=hostname $(jq -r ".instanceId" /run/instance-identity)
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

MODULES_HOOK = """\
#!/bin/sh

PREREQ=""

prereqs()
{
  echo "$PREREQ"
}

case $1 in
# get pre-requisites
prereqs)
  prereqs
  exit 0
  ;;
esac

. /usr/share/initramfs-tools/hook-functions

manual_add_modules overlay
"""


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
            options root=PARTUUID={root_partuuid} loop=root.squashfs console=ttyS0 break=premount verbose
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

def make_unit(name, contents, *, symlink="multi-user.target.wants"):
    units = "image/etc/systemd/system/"
    unit_file = Path(f"{units}/{name}")

    unit_file.write_text(contents)
    if symlink:
        Path(f"{units}/{symlink}/{name}").symlink_to(unit_file)


def customize_image():
    Path("image/etc/systemd/network/ena.network").write_text(ENA_UNIT)
    Path("image/etc/systemd/system/multi-user.target.wants/systemd-networkd.service").symlink_to("/lib/systemd/system/systemd-networkd.service")
    Path("image/efi").mkdir()
    Path("image/root/.ssh").mkdir()
    Path("image/root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKr4DFWVEoLCTgjtzl3wT+JnYnDojJAS/4hsFww4n/R8\n")

    for key in glob("image/etc/ssh/ssh_host_*_key"):
        filename = basename(key)
        os.remove(key)
        os.symlink("/var/lib/ssh/" + filename, key)

    for key in glob("image/etc/ssh/ssh_host_*_key.pub"):
        os.remove(key)

    Path("image/etc/fstab").write_text("none /tmp tmpfs defaults 0 0\n")

    units = "image/etc/systemd/system/"

    Path(f"{units}/ssh-keygen.service").write_text(SSH_KEYGEN_UNIT)
    Path(f"{units}/ssh.service.requires").mkdir()
    Path(f"{units}/ssh.service.requires/ssh-keygen.service").symlink_to("/etc/systemd/system/ssh-keygen.service")

    make_unit("imds.service", IMDS_UNIT)
    make_unit("hostname.service", HOSTNAME_UNIT)



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

    # apt lists are huge. They aren't costly at runtime (because they don't get 
    # decompressed until needed), but they make the images bigger and thus slower
    # to copy back and forth to S3
    rmtree("image/var/cache/apt")
    rmtree("image/var/lib/apt/lists")


def make_squashfs(squashfs_image):
    rm_f(squashfs_image)
    run([
        "mksquashfs", "image", squashfs_image,
        "-comp", "zstd", "-processors", "1",
        "-wildcards",
        "-e", "boot/*"
    ])

def write_script(filename, contents):
    path = Path(filename)
    path.write_text(contents)
    path.chmod(0o755)


def configure_initramfs(root_partuuid):
    Path("image/etc/initramfs-tools/initramfs.conf").write_text("MODULES=list\nCOMPRESS=lz4\n")

    overlay_script =dd("""\
    #!/bin/sh -e

    PREREQ=""
    prereqs() {
      echo "$PREREQ"
    }

    case ${1} in
      prereqs)
        prereqs
        exit 0
        ;;
    esac

    mkdir -p /run/overlay
    cd /run/overlay

    mkdir host immutable-root
    mount /dev/disk/by-partuuid/""" + root_partuuid + """ host
    mount -o move /root immutable-root
    mkdir -p host/state host/work
    mount -t overlay -o lowerdir=immutable-root,upperdir=host/state,workdir=host/work none /root
    """)
    write_script("image/usr/share/initramfs-tools/scripts/init-bottom/overlay", overlay_script)
    write_script("image/usr/share/initramfs-tools/hooks/copy-modules", MODULES_HOOK)

    # No need for microcode in a cloud guest
    Path("image/usr/share/initramfs-tools/hooks/intel_microcode").unlink()


def main():
    root_partuuid = str(uuid.uuid4())
    esp_uuid = str(uuid.uuid4())
    outfile = "image.raw"
    squashfs_image = "image.squashfs"

    run([
        "mkosi",
        "--force",
        "--repositories", "main,universe",
        "-d", "ubuntu",
        "-r", "hirsute",
        "-t", "directory",
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
        # This will get installed as a dependency of the kernel
        # We don't want it. We need to install it now so that we can disable it.
        "-p intel-microcode",
        "--debug", "run",
    ])
    change_passwords("image")
    configure_initramfs(root_partuuid)
    customize_image()
    extract_kernel()

    make_squashfs(squashfs_image)

    format_disk(esp_uuid, root_partuuid, outfile)
    set_up_boot(outfile, root_partuuid, esp_uuid)
    compress_product(outfile)


if __name__ == '__main__':
    main()
