from __future__ import annotations
from typing import Optional

import subprocess
import os
import getpass
import time
import argparse
import paramiko  # type: ignore
from rich.console import Console

console = Console()

basic_flake_nix = """
{
  description = "nixos-simple-deploy";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    pkg-nixos-simple-deploy.url = "github:jbuchermn/nixos-simple-deploy";
  };

  outputs = { nixpkgs, flake-utils, pkg-nixos-simple-deploy, ... }:
  let
    system = "x86_64-linux";
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
  in {
    nixosConfigurations = {
      HOSTNAME = nixosSystem [
        ./HOSTNAME/configuration.nix
        ({ config, pkgs, ... }: {
            # Place configuration here, or create new modules
        })
      ];
    }; 
  } // flake-utils.lib.eachDefaultSystem (system:
    let
      pkgs = import nixpkgs {
        inherit system;
        config = {
          allowUnfree = true;
        };
        overlays = [
          (self: super: rec {
            nixos-simple-deploy = pkg-nixos-simple-deploy.packages.${system}.nixos-simple-deploy;
          })
        ];
      };
    in {
      devShell = pkgs.mkShell {
        buildInputs = with pkgs; [
          nixos-simple-deploy
        ];
      };
    }
  );
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
        console.print("[yellow]> Running local: %s[/yellow]" % (" ".join(args)))
        result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
        if result.returncode != 0:
            raise Exception("Error executing local '%s': %s %s" % (" ".join(args), result.stdout, result.stderr))
        return result.stdout.strip()

    def _run_remote_cmd(self, args: list[str], cwd: Optional[str]=None, silent=False) -> str:
        if not silent:
            console.print("[cyan]> Running remote: %s[/cyan]" % (" ".join(args)))
        if cwd is not None:
            args = ["cd %s; " % cwd] + args
        stdin, stdout, stderr = self._ssh.exec_command(" ".join(args))
        def line_buffered(f):
            line_buf = ""
            while not f.channel.exit_status_ready():
                line_buf += f.read(1).decode("utf-8")
                if line_buf.endswith('\n') or line_buf.endswith('\r'):
                    yield line_buf
                    line_buf = ''

        result = ""
        for l in line_buffered(stdout):
            if not silent:
                console.print(l, end='')
            result += l

        if stdout.channel.recv_exit_status() != 0:
            raise Exception("Error executing remote '%s': '%s' '%s'" % (" ".join(args), result.strip(), "".join(stderr.readlines())))
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
            console.print("[bold magenta]WARNING! Git repo is dirty. Uncommitted changes will not be pushed[/bold magenta]")
        git_root = self._run_local_cmd(["git", "rev-parse", "--show-toplevel"], cwd=self.dir)
        return git_root

    def _push_remote_git(self, force: bool=False) -> bool:
        if force:
            console.print("[bold cyan]Cleaning bare repo...[/bold cyan]")
            self._run_remote_cmd(["mkdir", "-p", "/etc/nixos-simple-deploy"])
            self._run_remote_cmd(["rm", "-rf", "bare"], cwd="/etc/nixos-simple-deploy")
            self._run_remote_cmd(["mkdir", "bare"], cwd="/etc/nixos-simple-deploy")

            console.print("[bold cyan]Creating bare repo...[/bold cyan]")
            self._run_remote_cmd(["git", "init", "--bare"], cwd="/etc/nixos-simple-deploy/bare")

        self._run_local_cmd(["git", "push", "ssh://%s@%s/etc/nixos-simple-deploy/bare" % (self.user, self.host)])
        return True

    def _update_remote_git(self, force: bool=False) -> bool:
        if force:
            try:
                self._run_remote_cmd(["rm", "-rd", "/etc/nixos-simple-deploy/working-dir"])
            except:
                pass

        try:
            console.print("[bold cyan]Cloning repo...[/bold cyan]")
            self._run_remote_cmd(["git", "clone", "./bare", "./working-dir"], cwd="/etc/nixos-simple-deploy")
        except:
            console.print("[bold cyan]...not necessary[/bold cyan]")
            # exists
            pass

        console.print("Pulling...")
        try:
            gst = self._run_remote_cmd(["git", "status", "--porcelain"], cwd="/etc/nixos-simple-deploy/working-dir")
            if gst.strip() != "" and not force:
                console.print("[bold magenta]ERROR! Working directory on deployment is dirty - aborting[/bold magenta]")
                return False

            branch = self._run_local_cmd(["git", "branch", "--show-current"], cwd=self.dir)
            self._run_remote_cmd(["git", "fetch"], cwd="/etc/nixos-simple-deploy/working-dir")
            self._run_remote_cmd(["git", "reset", "--hard", "origin/%s" % branch], cwd="/etc/nixos-simple-deploy/working-dir")
        except Exception:
            return False

        return True

    def run_ssh_store_key(self) -> None:
        self._run_local_cmd(["ssh-copy-id", "%s@%s" % (self.user, self.host)])

    def run_bootstrap(self, hostname: str, root_password: Optional[str]=None, dont_prompt: bool=False) -> None:
        vol = "/dev/sda"
        swap = 8

        console.print("[bold]Bootstrapping via SSH[/bold]")
        console.print("  - Boot:     [i]MBR[/i]")
        console.print("  - Volume:   [i]%s[/i]" % vol)
        console.print("  - Swap:     [i]%dGB[/i]" % swap)
        console.print("  - Hostname: [i]%s[/i]" % hostname)
        if input("OK? (yes/no) ") != "yes":
            console.print("[bold magenta]ERROR! Aborting[/bold magenta]")
            return

        for cmd in [
                "parted %s -s -- mklabel msdos" % vol,
                "parted %s -s -- mkpart primary 1MiB -%dGiB" % (vol, swap),
                "parted %s -s -- mkpart primary linux-swap -%dGiB 100%%" % (vol, swap),
                "mkfs.ext4 -L nixos %s1" % vol,
                "mkswap -L swap %s2" % vol,
                "mount /dev/disk/by-label/nixos /mnt",
                "nixos-generate-config --root /mnt 2>&1"]:
            self._run_remote_cmd(cmd.split(" "))
            time.sleep(0.5)  # Allow for /dev/disk-by-label to appear

        self._run_local_cmd(["mkdir", "-p", hostname])
        self._copy_file_to_local("/mnt/etc/nixos/configuration.nix", os.path.join(self.dir, hostname, "configuration.nix"))
        self._copy_file_to_local("/mnt/etc/nixos/hardware-configuration.nix", os.path.join(self.dir, hostname, "hardware-configuration.nix"))

        conf_nix: list[str] = []
        with open(os.path.join(self.dir, hostname, "configuration.nix"), 'r') as f:
            for l in f:
                if "boot.loader.grub.device" in l:
                    l = "  " + l[4:]  # Uncomment
                elif "networking.hostName" in l:
                    l = "  networking.hostName = \"%s\";\n" % hostname
                elif "environment.systemPackages" in l:
                    l = "  environment.systemPackages = with pkgs; [ git ];\n"
                elif l.startswith("}"):
                    conf_nix += [
                            "  services.openssh = {\n",
                            "      enable = true;\n",
                            "      permitRootLogin = \"yes\";\n",
                            "  };\n\n",
                            "  nix = {\n",
                            "    package = pkgs.nixFlakes;\n",
                            "    extraOptions = ''\n",
                            "       experimental-features = nix-command flakes\n",
                            "   '';\n",
                            "  };\n",
                        ]
                conf_nix += [l]

        with open(os.path.join(self.dir, hostname, "configuration.nix"), 'w') as f:
            f.writelines(conf_nix)

        if not dont_prompt:
            console.print("[bold]Prepared configuration.nix with hostname and SSH[/bold]")
            console.print("  - Feel free to adjust other configuration options")
            console.print("  - Possibly you need to [i]adjust some boot settings[/i] for MBR")
            input("Hit enter to continue...")
        self._copy_file_to_remote(os.path.join(self.dir, hostname, "configuration.nix"), "/mnt/etc/nixos/configuration.nix")
        self._copy_file_to_remote(os.path.join(self.dir, hostname, "hardware-configuration.nix"), "/mnt/etc/nixos/hardware-configuration.nix")

        console.print("[bold cyan]Installing...[/bold cyan]")
        self._run_remote_cmd(["nixos-install", "--no-root-passwd", "2>&1"])

        password = "" if root_password is None else root_password
        password_check = "dummy" if root_password is None else root_password
        while password != password_check:
            password = getpass.getpass("Enter root password: ")
            password_check = getpass.getpass("Repeat root password: ")
        console.print("[bold cyan]Setting root password...[/bold cyan]")
        self._run_remote_cmd(["sh", "-c", "'echo root:%s | chpasswd --root /mnt root'" % password])

        console.print("[bold cyan]Rebooting...[/bold cyan]")
        try:
            self._run_remote_cmd(["reboot"])
        except:
            pass

        self._reset_ssh_nonsense()

    def run_create_deployment(self) -> None:
        hostname = self._run_remote_cmd(["hostname"]).strip()
        console.print("Remote hostname is: [bold]'%s'[/bold]" % hostname)
        if not os.path.isfile(os.path.join(hostname, "configuration.nix")):
            console.print("[bold cyan]Setting up configuration.nix and hardware-configuration.nix in %s[/bold cyan]" % hostname)
            self._run_local_cmd(["mkdir", "-p", hostname])
            self._copy_file_to_local("/etc/nixos/configuration.nix", os.path.join(hostname, "configuration.nix"))
            self._copy_file_to_local("/etc/nixos/hardware-configuration.nix", os.path.join(hostname, "hardware-configuration.nix"))

            console.print("""
      - Make sure the [i]hostname[/i] is set correctly in configuration.nix")
      - Make sure configuration.nix allows for [i]SSH connection[/i], e.g.")
            services.openssh = {
                enable = true;
                permitRootLogin = "yes";
            };
            """)

        else:
            console.print("[bold cyan]...configuration.nix appears to be present, skipping download[/bold cyan]")

        if not os.path.isfile(os.path.join(self.dir, "flake.nix")):
            console.print("[bold cyan]Creating flake.nix[/bold cyan]")
            with open(os.path.join(self.dir, "flake.nix"), "w") as flake:
                flake.write(basic_flake_nix.replace("HOSTNAME", hostname))
        else:
            console.print("[bold cyan]...flake.nix is present\n  - Be sure to [i]add new deployment[/i] '%s' to it" % hostname)

        self._push_remote_git(True)

    def run_deploy(self, force: bool=False) -> None:
        if not self._push_remote_git(force):
            console.print("[bold magenta]ERROR! Aborting[/bold magenta]")
            return
        if not self._update_remote_git(force):
            console.print("[bold magenta]ERROR! Aborting[/bold magenta]")
            return

        git_root = self._check_local_git()
        flake_path = os.path.relpath(os.path.abspath(self.dir), git_root)

        console.print("[bold cyan]Rebuilding NixOS...[/bold cyan]")
        self._run_remote_cmd(["nixos-rebuild", "switch", "--flake", ".#", "2>&1"], cwd="/etc/nixos-simple-deploy/working-dir/" + flake_path)
        console.print("[bold cyan]...done[/bold cyan]")

    def run_pull(self) -> None:
        self._run_local_cmd(["git", "pull", "ssh://%s@%s/etc/nixos-simple-deploy/bare" % (self.user, self.host)])

    def run_ssh(self) -> None:
        os.system("ssh %s@%s" % (self.user, self.host))

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="Hostname / IP address of deployment", type=str)
    parser.add_argument("command", help="Command", type=str, choices=["store-key", "ssh", "bootstrap", "create", "deploy", "pull"])
    parser.add_argument("hostname", help="Hostname for command bootstrap", type=str, nargs='?')
    parser.add_argument("-p", "--password", help="Password to connect to deployment via SSH", type=str)
    parser.add_argument("-P", "--root-password", help="Root password for bootstrapping", type=str)
    parser.add_argument("-f", "--force", help="Force, possibly overwriting data", action="store_true")
    args = parser.parse_args()
    deployed = Deployed(args.host, args.password)
    if args.command == "store-key":
        deployed.run_ssh_store_key()
    elif args.command == "ssh":
        deployed.run_ssh()
    elif args.command == "bootstrap":
        deployed.run_bootstrap(args.hostname, root_password=args.root_password, dont_prompt=args.force)
    elif args.command == "create":
        deployed.run_create_deployment()
    elif args.command == "deploy":
        deployed.run_deploy(args.force)
    elif args.command == "pull":
        deployed.run_pull()

if __name__ == '__main__':
    main()
