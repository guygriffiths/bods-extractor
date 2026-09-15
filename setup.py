from setuptools import setup, find_packages

setup(
    name="bods_extractor",
    version="0.2.0",
    description="UK bus fare extraction from the Bus Open Data Service",
    packages=find_packages(exclude=("tools", "tests")),
    python_requires=">=3.10",
    install_requires=[
        "click>=8.1",
        "lxml>=5.0",
        "pyproj>=3.6",
    ],
    extras_require={
        # Only needed for the Google Directions demonstration front-end.
        "demo": ["requests>=2.31", "python-dotenv>=1.0"],
    },
    entry_points={"console_scripts": ["bodsDB=bods_extractor.cli:cli"]},
)
