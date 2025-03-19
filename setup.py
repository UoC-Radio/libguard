from setuptools import setup, find_packages

setup(
    name="libguard",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "tqdm",
        "python-magic",
        "aunalyzer",
        "mutagen",
        "requests",
    ],
    entry_points={
        "console_scripts": [
            "libguard=libguard.__main__:main",
        ],
    },
    python_requires=">=3.9",
    author="Nick Kossifidis",
    author_email="mickflemm@gmail.com",
    description="A music library organization and verification tool",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://rastapank.radio.uoc.gr",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)",
        "Operating System :: OS Independent",
    ],
)
