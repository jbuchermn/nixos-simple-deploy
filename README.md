# nixos-simple-deploy

A small helper script to manage nixOS deployments via SSH and git. This doesn't rely on `nix-instantiate` or `nix-copy-closure`, instead building the config is done completely on the deployment target.

## Simnple setup

Start a VM running NixOS live iso, set a root password, note the IP.

```sh
cd my-deployment
git init
nixos-simple-deploy 192.168.x.y bootstrap deployment-hostname -p the-password
nixos-simple-deploy 192.168.x.y store-key
nixos-simple-deploy 192.168.x.y create
vim flake.nix  # place your desired server config
git commit -am'Initial deployment'
nixos-simple-deploy 192.168.x.y deploy
```
