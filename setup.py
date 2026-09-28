from setuptools import setup, find_packages

setup(
    name="po_validation",
    version="0.1.0",
    description="F5 Sales Operations (SOS) Purchase Order Validation & Booking Engine",
    packages=find_packages(),
    package_data={
        "": ["*.yaml", "*.yml", "*.json"],
    },
    include_package_data=True,
    install_requires=[
        "pypdf>=4.0.0",
        "pdfplumber>=0.10.0",
        "pyyaml>=6.0",
        "requests>=2.28.0",
        "snowflake-connector-python>=3.0.0",
    ],
    python_requires=">=3.9",
)
