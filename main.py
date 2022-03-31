from __future__ import annotations
from typing import Optional

import subprocess
import os
import getpass
import time
import paramiko  # type: ignore

basic_flake_nix = """
{
  description = "nixos-simple-deploy";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, ... }:
  {
    packages = flake-utils.lib.eachDefaultSystem (system:
      let
        stateVersion = "22.05";

        pkgs = import nixpkgs {
          inherit system;
          config = {
            allowUnfree = true;
          };
          overlays = [
          ];
        };

        lib = nixpkgs.lib;

        nixosSystem = _modules: lib.nixosSystem {
            inherit system pkgs;
            modules = [ 
              ({ config, pkgs, ... }: {
                nix.registry.nixpkgs.flake = nixpkgs;
              })
            ] ++ _modules;
        };
      in
      {
        nixosConfigurations = {
          HOSTNAME = nixosSystem [
            ./HOSTNAME/configuration.nix
            ({ config, pkgs, ... }: {
                # Place configuration here, or create new modules in ./HOSTNAME
            })
          ];
        }; 
    );
  };
}

"""

class Deployed:
    def __init__(self, host: str, password: str) -> None:
        self.host = host
        self.user = "root"
        self.password = password
        self.dir = "."

        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(hostname=self.host, username=self.user, password=self.password)

    def _run_local_cmd(self, args: list[str], cwd: Optional[str]=None, silent=True) -> str:
        result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
        if result.returncode != 0:
            raise Exception("Error executing local '%s': %s %s" % (" ".join(args), result.stdout, result.stderr))
        return result.stdout.strip()

    def _run_remote_cmd(self, args: list[str], cwd: Optional[str]=None, silent=False) -> str:
        if cwd is not None:
            args = ["cd %s; " % cwd] + args
        stdin, stdout, stderr = self._ssh.exec_command(" ".join(args))
        def line_buffered(f):
            line_buf = ""
            while not f.channel.exit_status_ready():
                line_buf += f.read(1).decode("utf8")
                if line_buf.endswith('\n'):
                    yield line_buf
                    line_buf = ''

        result = ""
        for l in line_buffered(stdout):
            if not silent:
                print(l, end='')
            result += l

        if stdout.channel.recv_exit_status() != 0:
            raise Exception("Error executing remote '%s': '%s' '%s'" % (" ".join(args), result.strip(), stderr.readlines()))
        return result.strip()

    def _copy_file_to_remote(self, local: str, remote: str) -> None:
        ftp_client = self._ssh.open_sftp()
        ftp_client.put(local, remote)
        ftp_client.close()

    def _copy_file_to_local(self, remote: str, local: str) -> None:
        ftp_client = self._ssh.open_sftp()
        ftp_client.get(remote, local)
        ftp_client.close()

    def _reset_ssh_nonsense(self) -> None:
        self._run_local_cmd(["ssh-keygen", "-R", self.host])

    def _check_local_git(self) -> str:
        gst = self._run_local_cmd(["git", "status", "--porcelain"], cwd=self.dir)
        if gst.strip() != "":
            print("WARNING! Git repo is dirty. Uncommitted changes will not be pushed")
        git_root = self._run_local_cmd(["git", "rev-parse", "--show-toplevel"], cwd=self.dir)
        return git_root

    def _push_remote_git(self, force: bool=False) -> bool:
        if force:
            git_root = self._check_local_git()
            tmp_dir = self._run_local_cmd(["mktemp", "-d"])

            print("Compressing git repo to tar...")
            self._run_local_cmd(["tar", "cfz", os.path.join(tmp_dir, "git.tar.gz"), "."], cwd=os.path.join(git_root, ".git")) 

            print("Copying to deployment...")
            self._run_remote_cmd(["mkdir", "-p", "/etc/nixos-simple-deploy"])
            self._copy_file_to_remote(os.path.join(tmp_dir, "git.tar.gz"), "/etc/nixos-simple-deploy/git.tar.gz")

            print("Setting up bare git repo...")
            self._run_remote_cmd(["rm", "-rf", "bare"], cwd="/etc/nixos-simple-deploy")
            self._run_remote_cmd(["mkdir", "bare"], cwd="/etc/nixos-simple-deploy")
            self._run_remote_cmd(["tar", "xfz", "../git.tar.gz"], cwd="/etc/nixos-simple-deploy/bare")
            self._run_remote_cmd(["rm", "./git.tar.gz"], cwd="/etc/nixos-simple-deploy")
        else:
            self._run_local_cmd(["git", "push", "ssh://%s@%s/etc/nixos-simple-deploy/bare" % (self.user, self.host)])
        return True

    def _update_remote_git(self, force: bool=False) -> bool:
        if force:
            try:
                self._run_remote_cmd(["rm", "-rd", "/etc/nixos-simple-deploy/working-dir"])
            except:
                pass

        try:
            print("Cloning repo...")
            self._run_remote_cmd(["git", "clone", "./bare", "./working-dir"], cwd="/etc/nixos-simple-deploy")
        except:
            print("...not necessary")
            # exists
            pass

        print("Pulling...")
        try:
            self._run_remote_cmd(["git", "pull"], cwd="/etc/nixos-simple-deploy/working-dir")
        except Exception as e:
            print(e)
            return False

        return True

    def run_ssh_copyid(self) -> None:
        self._run_local_cmd(["ssh-copy-id", "%s@%s" % (self.user, self.host)])

    def run_bootstrap(self, hostname: str) -> None:
        vol = "/dev/sda"
        swap = 8

        print("Bootstrapping via SSH")
        print("  - Boot: MBR")
        print("  - Volume: %s" % vol)
        print("  - Swap: %dGB" % swap)
        print("  - Hostname: %s" % hostname)
        if input("OK? (yes/no) ") != "yes":
            print("Aborting")
            return

        for cmd in [
                "parted %s -s -- mklabel msdos" % vol,
                "parted %s -s -- mkpart primary 1MiB -%dGiB" % (vol, swap),
                "parted %s -s -- mkpart primary linux-swap -%dGiB 100%%" % (vol, swap),
                "mkfs.ext4 -L nixos %s1" % vol,
                "mkswap -L swap %s2" % vol,
                "mount /dev/disk/by-label/nixos /mnt",
                "nixos-generate-config --root /mnt"]:
            print("Executing '%s'..." % cmd)
            self._run_remote_cmd(cmd.split(" "))
            time.sleep(0.5)  # Allow for /dev/disk-by-label to appear

        self._run_local_cmd(["mkdir", "-p", hostname])
        self._copy_file_to_local("/mnt/etc/nixos/configuration.nix", os.path.join(self.dir, hostname, "configuration.nix"))
        self._copy_file_to_local("/mnt/etc/nixos/hardware-configuration.nix", os.path.join(self.dir, hostname, "hardware-configuration.nix"))

        conf_nix: list[str] = []
        with open(os.path.join(self.dir, hostname, "configuration.nix"), 'r') as f:
            for l in f:
                if "networking.hostName" in l:
                    l = "  networking.hostName = \"%s\";\n" % hostname
                elif "environment.systemPackages" in l:
                    l = "  environment.systemPackages = with pkgs; [ git ];\n"
                elif l.startswith("}"):
                    conf_nix += [
                            "  services.openssh = {\n",
                            "      enable = true;\n",
                            "      permitRootLogin = \"yes\";\n",
                            "  };\n"
                        ]
                conf_nix += [l]

        with open(os.path.join(self.dir, hostname, "configuration.nix"), 'w') as f:
            f.writelines(conf_nix)

        print("Prepared configuration.nix with hostname and SSH")
        print("  - Feel free to adjust other configuration options")
        print("  - Probably you need to adjust some boot settings for MBR")
        input("Hit enter to continue...")
        self._copy_file_to_remote(os.path.join(self.dir, hostname, "configuration.nix"), "/mnt/etc/nixos/configuration.nix")
        self._copy_file_to_remote(os.path.join(self.dir, hostname, "hardware-configuration.nix"), "/mnt/etc/nixos/hardware-configuration.nix")

        print("Installing...")
        self._run_remote_cmd(["nixos-install", "--no-root-passwd"])

        password = ""
        password_check = "dummy"
        while password != password_check:
            password = getpass.getpass("Enter root password: ")
            password_check = getpass.getpass("Repeat root password: ")
        print("Setting root password...")
        self._run_remote_cmd(["sh", "-c", "'echo root:%s | chpasswd --root /mnt root'" % password])

        print("Rebooting...")
        try:
            self._run_remote_cmd(["reboot"])
        except:
            pass

        self._reset_ssh_nonsense()

    def run_create_deployment(self) -> None:
        hostname = self._run_remote_cmd(["hostname"]).strip()
        print("Remote hostname is: '%s'" % hostname)
        if not os.path.isfile(os.path.join(hostname, "configuration.nix")):
            print("Setting up configuration.nix and hardware-configuration.nix in %s" % hostname)
            self._run_local_cmd(["mkdir", "-p", hostname])
            self._copy_file_to_local("/etc/nixos/configuration.nix", os.path.join(hostname, "configuration.nix"))
            self._copy_file_to_local("/etc/nixos/hardware-configuration.nix", os.path.join(hostname, "hardware-configuration.nix"))

            print("""
      - Make sure the hostname is set correctly in configuration.nix")
      - Make sure configuraiton.nix allows for SSH connection, e.g.")
            services.openssh = {
                enable = true;
                permitRootLogin = "yes";
            };
            """)

        else:
            print("configuration.nix appears to be present, skipping download")

        if not os.path.isfile(os.path.join(self.dir, "flake.nix")):
            print("Creating flake.nix")
            with open(os.path.join(self.dir, "flake.nix"), "w") as flake:
                flake.write(basic_flake_nix.replace("HOSTNAME", hostname))
        else:
            print("flake.nix is present\n  - Be sure to add new deployment '%s' to it" % hostname)

        self._push_remote_git(True)

    def run_deploy(self, force: bool=False) -> None:
        if not self._push_remote_git(force):
            print("ERROR! Aborting")
            return
        if not self._update_remote_git(force):
            print("ERROR! Aborting")
            return

        git_root = self._check_local_git()
        flake_path = os.path.relpath(os.path.abspath(self.dir), git_root)

        print("Rebuilding NixOS...")
        result = self._run_remote_cmd(["nixos-rebuild", "switch", "--flake", ".#"], cwd="/etc/nixos-simple-deploy/working-dir/" + flake_path)
        print(result)
        print("...done")

    def run_pull(self) -> None:
        self._run_local_cmd(["git", "pull", "ssh://%s@%s/etc/nixos-simple-deploy/bare" % (self.user, self.host)])

    def run_ssh(self) -> None:
        os.system("ssh %s@%s" % (self.user, self.host))

if __name__ == '__main__':
    Deployed("192.168.64.2", "test")\
            .run_ssh_copyid()
            # .run_pull()
            # .run_deploy()
            # .run_create_deployment()
            # .run_bootstrap("nixos-simple-deploy-test")

    # TODO
    # proper formatting
    # package setup.py / argparse
