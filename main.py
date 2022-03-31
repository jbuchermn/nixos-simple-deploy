from __future__ import annotations
from typing import Any, Optional

import subprocess
import shutil
import os
import spur  # type: ignore

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

def run_local_cmd(args: list[str], cwd: Optional[str]=None) -> str:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise Exception("Error executing local '%s': %s %s" % (" ".join(args), result.stdout, result.stderr))
    return result.stdout.strip()

def run_remote_cmd(shell: Any, args: list[str], cwd: Optional[str]=None) -> str:
    try:
        result = shell.run(args, cwd=cwd)
        if result.return_code != 0:
            raise Exception("Error executing remote '%s': %s %s" % (" ".join(args), result.output.decode("ascii"), result.stderr_output.decode("ascii")))
        return result.output.decode("ascii").strip()
    except spur.results.RunProcessError as e:
        raise Exception("While executing remote '%s': \n%s" % (" ".join(args), e.stderr_output.decode("ascii")))

def copy_file_to_remote(shell: Any, local: str, remote: str) -> None:
    with shell.open(remote, "wb") as remote_file:
        with open(local, "rb") as local_file:
            shutil.copyfileobj(local_file, remote_file)

def copy_file_to_local(shell: Any, remote: str, local: str) -> None:
    with shell.open(remote, "rb") as remote_file:
        with open(local, "wb") as local_file:
            shutil.copyfileobj(remote_file, local_file)


def push_remote_git(shell: Any, dir: str) -> bool:
    gst = run_local_cmd(["git", "status", "--porcelain"], cwd=dir)
    if gst.strip() != "":
        print("WARNING! Git repo is dirty. Uncommitted changes will not be pushed")
    git_root = run_local_cmd(["git", "rev-parse", "--show-toplevel"], cwd=dir)
    tmp_dir = run_local_cmd(["mktemp", "-d"])

    print("Compressing git repo to tar...")
    run_local_cmd(["tar", "cfz", os.path.join(tmp_dir, "git.tar.gz"), "."], cwd=os.path.join(git_root, ".git")) 

    print("Copying to deployment...")
    run_remote_cmd(shell, ["mkdir", "-p", "/etc/nixos-simple-deploy"])
    copy_file_to_remote(shell, os.path.join(tmp_dir, "git.tar.gz"), "/etc/nixos-simple-deploy/git.tar.gz")

    print("Setting up bare git repo...")
    run_remote_cmd(shell, ["rm", "-rf", "bare"], cwd="/etc/nixos-simple-deploy")
    run_remote_cmd(shell, ["mkdir", "bare"], cwd="/etc/nixos-simple-deploy")
    run_remote_cmd(shell, ["tar", "xfz", "../git.tar.gz"], cwd="/etc/nixos-simple-deploy/bare")
    run_remote_cmd(shell, ["rm", "./git.tar.gz"], cwd="/etc/nixos-simple-deploy")

    return True


def update_remote_git(shell: Any, dir: str, force: bool=False) -> bool:
    if force:
        try:
            run_remote_cmd(shell, ["rm", "-rd", "/etc/nixos-simple-deploy/working-dir"])
        except:
            pass

    try:
        print("Cloning repo...")
        run_remote_cmd(shell, ["git", "clone", "./bare", "./working-dir"], cwd="/etc/nixos-simple-deploy")
    except:
        print("...not necessary")
        # exists
        pass

    print("Pulling...")
    try:
        run_remote_cmd(shell, ["git", "pull"], cwd="/etc/nixos-simple-deploy/working-dir")
    except Exception as e:
        print(e)
        return False

    return True


def run_create_deployment(shell: Any, dir: str) -> None:
    hostname = run_remote_cmd(shell, ["hostname"]).strip()
    print("Remote hostname is: '%s'" % hostname)
    print("Setting up configuration.nix and hardware-configuration.nix in %s" % hostname)
    run_local_cmd(["mkdir", "-p", hostname])
    copy_file_to_local(shell, "/etc/nixos/configuration.nix", os.path.join(hostname, "configuration.nix"))
    copy_file_to_local(shell, "/etc/nixos/hardware-configuration.nix", os.path.join(hostname, "hardware-configuration.nix"))
    print("""
  - Make sure the hostname is set correctly in configuration.nix")
  - Make sure configuraiton.nix allows for SSH connection, e.g.")
        services.openssh = {
            enable = true;
            permitRootLogin = "yes";
        };
    """)

    if not os.path.isfile(os.path.join(dir, "flake.nix")):
        print("Creating flake.nix")
        with open(os.path.join(dir, "flake.nix"), "w") as flake:
            flake.write(basic_flake_nix.replace("HOSTNAME", hostname))
    else:
        print("flake.nix is present\n  - Be sure to add new deployment '%s' to it" % hostname)


def run_deploy(shell: Any, dir: str, force: bool=False) -> None:
    if not push_remote_git(shell, dir):
        print("ERROR! Aborting")
        return
    if not update_remote_git(shell, dir, force):
        print("ERROR! Aborting")
        return
    print("Rebuilding NixOS...")
    result = run_remote_cmd(shell, ["nixos-rebuild", "switch", "--flake", ".#"], cwd="/etc/nixos-simple-deploy/working-dir/???")
    print(result)
    print("...done")

if __name__ == '__main__':
    shell = spur.SshShell(
        hostname="192.168.178.64",
        username="root",
        password="passwd"
    )

    # run_create_deployment(shell, ".")
    run_deploy(shell, ".")

    # ssh-copy-id
    # pull remote git
    # bootstrap
