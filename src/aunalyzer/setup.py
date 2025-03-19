from setuptools import setup, Extension
import subprocess

def get_pkg_config_flags(packages):
	"""Get compile and link flags from pkg-config for a list of packages"""
	try:
		cflags = subprocess.check_output(['pkg-config', '--cflags'] + packages).decode('utf-8').strip().split()
		libs = subprocess.check_output(['pkg-config', '--libs'] + packages).decode('utf-8').strip().split()
		return cflags, libs
	except (subprocess.SubprocessError, FileNotFoundError):
		print(f"Warning: pkg-config failed. Using default flags.")
		return [], []

# Get ffmpeg and libebur128 flags
packages = ['libavcodec', 'libavformat', 'libavutil', 'libswresample', 'libebur128']
cflags, libs = get_pkg_config_flags(packages)

# Define the extension module
aunalyzer_module = Extension(
	'aunalyzer._aunalyzer',
	sources=['src/aunalyzer.c'],
	extra_compile_args=cflags,
	extra_link_args=libs,
)

setup(
	name="aunalyzer",
	version="0.1.0",
	description="Python module for audio file analysis using FFmpeg and libebur128",
	author="Nick Kossifidis",
	author_email="mickflemm@gmail.com",
	url="https://radio.uoc.gr/",
	packages=['aunalyzer'],
	package_dir={'aunalyzer': '.'},
	ext_modules=[aunalyzer_module],
	license="GPL-2.0-or-later",
	classifiers=[
		"Development Status :: 4 - Beta",
		"Intended Audience :: Developers", 
		"License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)",
		"Programming Language :: Python :: 3",
		"Programming Language :: C",
		"Topic :: Multimedia :: Sound/Audio :: Analysis",
	],
)