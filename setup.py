
from setuptools import setup

setup(name='nixos-simple-deploy',
      version='0.1',
      description='Straightforward ssh-based bootstrapping of and deployment to nixes machines',
      url="https://github.com/jbuchermn/nixos-simple-deploy",
      author='Jonas Bucher',
      author_email='j.bucher.mn@gmail.com',
      packages=['nixos_simple_deploy'],
      package_data={},
      scripts=['bin/nixos-simple-deploy'],
      install_requires=[
          'paramiko',
      ])
