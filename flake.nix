{
  description = "NixOS simple deploy";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
  flake-utils.lib.eachDefaultSystem (
    system:
    let
      pkgs = import nixpkgs {
        inherit system;
      };
    in
    {
      packages.nixos-simple-deploy =
        pkgs.python3.pkgs.buildPythonApplication rec {
          pname = "nixos-simple-deploy";
          version = "0.1";

          src = ./.;

          propagatedBuildInputs = with pkgs.python3Packages; [
            paramiko
            rich
            setuptools
          ];
        };

      devShell = let
        my-python = pkgs.python3;
        python-with-my-packages = my-python.withPackages (ps: with ps; [
          paramiko
          rich

          python-lsp-server
          (pylsp-mypy.overrideAttrs (old: { pytestCheckPhase = "true"; }))
          mypy
        ]);
      in
        pkgs.mkShell {
          buildInputs = [ python-with-my-packages ];
        };
    }
  );
}
