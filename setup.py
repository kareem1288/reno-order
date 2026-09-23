# Compatibility shim for tools that still look for setup.py.
# The real build config is pyproject.toml (flit_core); bench / pip use that and ignore this file.

import re
from pathlib import Path

from setuptools import find_packages, setup

name = "reno_order"
version = re.search(
	r'^__version__\s*=\s*["\']([^"\']+)["\']',
	(Path(__file__).parent / name / "__init__.py").read_text(),
	re.MULTILINE,
).group(1)

setup(
	name=name,
	version=version,
	description="Kitchen Renovation Platform",
	author="Shaik Khaja Kareem",
	author_email="skkareem498@gmail.com",
	license="MIT",
	packages=find_packages(),
	include_package_data=True,
	zip_safe=False,
	python_requires=">=3.14",
)
