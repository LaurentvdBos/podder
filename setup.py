from setuptools import find_packages, setup

setup(name="podder",
      version="1.0.0",
      author="Laurent van den Bos",
      author_email="laurentvdbos@outlook.com",
      license="MIT",
      packages=find_packages(include=['podder']),
      entry_points={
            'console_scripts': ['podder=podder.__main__:main']
      })