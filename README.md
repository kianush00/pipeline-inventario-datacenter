# Datacenter Inventory Pipeline

![Datacenter Inventory Pipeline](./assets/flujo_inventario_datacenter.png)

## Overview

The **Datacenter Inventory Pipeline** is an automated solution designed to collect, process, and consolidate infrastructure information from datacenter nodes into a centralized inventory.

The pipeline uses **Rundeck** to orchestrate the collection of node information and generates structured outputs that are subsequently processed through a **Python-based ETL pipeline**. The ETL process cleans, transforms, validates, and consolidates the collected data into a consistent inventory format.

The goal is to provide a reliable and repeatable mechanism for maintaining up-to-date datacenter infrastructure information while reducing manual data collection and processing.

## Architecture

The pipeline consists of the following main stages:

1. **Node Information Collection**
   - Rundeck orchestrates jobs across datacenter nodes.
   - Relevant system and infrastructure information is collected.
   - The collected information is exported as structured data.

2. **Data Extraction**
   - The Python ETL process reads the generated data.
   - Multiple input sources and formats can be processed.

3. **Data Cleaning**
   - Invalid or incomplete records are identified.
   - Formatting inconsistencies are normalized.
   - Duplicate or redundant information can be handled.

4. **Data Transformation**
   - Raw node information is converted into a standardized schema.
   - Fields are normalized and derived attributes can be generated.

5. **Data Consolidation**
   - Processed information is combined into a centralized inventory.
   - The resulting dataset provides a consistent view of the datacenter infrastructure.

6. **Inventory Output**
   - The final inventory is generated in a structured format suitable for further consumption, reporting, automation, or integration with other systems.

## Workflow

```text
Datacenter Nodes
       |
       v
    Rundeck
       |
       v
 Structured Raw Data
       |
       v
    Python ETL
       |
       +--> Extract
       +--> Clean
       +--> Validate
       +--> Transform
       +--> Consolidate
       |
       v
Centralized Inventory
```

## Repository Structure

```text
.
├── assets/
│   └── flujo_inventario_datacenter.png
├── rundeck/
│   └── ...
├── etl/
│   └── ...
├── data/
│   ├── input/
│   └── output/
├── tests/
│   └── ...
├── requirements.txt
└── README.md
```

## Requirements

- Rundeck
- Python 3.x
- Python dependencies listed in `requirements.txt`
- Access to the target datacenter nodes
- Appropriate credentials and permissions for information collection

## Installation

Clone the repository:

```bash
git clone <repository-url>
cd <repository-directory>
```

Create a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the required dependencies:

```bash
pip install -r requirements.txt
```

Configure the Rundeck jobs and required credentials according to your environment.

## Configuration

Common configuration areas include:

- Rundeck node definitions
- Authentication and credentials
- Data collection commands
- Input and output directories
- ETL parameters
- Inventory schema
- Logging configuration

Do not store credentials, passwords, tokens, or other sensitive information directly in the repository.

## Usage

### 1. Collect Node Information

Execute the appropriate Rundeck job to collect information from the target nodes.

The collection process generates the structured input data required by the ETL pipeline.

### 2. Run the ETL Pipeline

Execute the Python ETL using the project's configured entry point.

Example:

```bash
python etl/main.py
```

The actual command may vary depending on the project structure.

### 3. Review the Inventory

The processed inventory is generated in the configured output location and can be consumed by downstream systems, reporting tools, or automation workflows.

## Data Processing Flow

```text
Raw Data
   |
   v
Extraction
   |
   v
Validation
   |
   v
Cleaning
   |
   v
Transformation
   |
   v
Consolidation
   |
   v
Centralized Inventory
```

## Logging and Troubleshooting

When troubleshooting the pipeline, check:

1. Rundeck job execution status.
2. Connectivity to target nodes.
3. Generated input files.
4. Input data format and completeness.
5. Python ETL logs.
6. Validation or transformation errors.
7. Generated inventory files.

## Testing

For Python projects using `pytest`:

```bash
pytest
```

Tests should cover, when applicable:

- Node data collection
- Input parsing
- Data normalization
- Transformation rules
- Duplicate handling
- Final inventory generation

## Security Considerations

The pipeline interacts with infrastructure systems and may process sensitive operational information.

Recommended practices include:

- Do not commit credentials or secrets.
- Use environment variables or a secrets-management solution.
- Apply least-privilege access.
- Restrict access to generated inventory data.
- Secure communication between components.
- Review Rundeck job permissions regularly.
- Avoid exposing sensitive node information in logs.

## Extensibility

The architecture can be extended to support:

- Additional datacenter environments
- New node information sources
- Additional data formats
- New inventory fields
- CMDB or asset-management integrations
- Automated data-quality checks
- Scheduled inventory generation
- Additional reporting and export formats

## Contributing

Contributions are welcome.

Before submitting changes:

1. Create a dedicated branch.
2. Keep changes focused and documented.
3. Add or update tests when applicable.
4. Verify that existing functionality continues to work.
5. Document relevant configuration or behavior changes.

## License

This project is distributed under the license specified in the repository.

If no license has been defined yet, add an appropriate `LICENSE` file before distributing the project.

## Disclaimer

This project is intended to automate infrastructure inventory collection and data processing. Adapt the configuration, collection methods, security controls, and deployment procedures to the requirements and policies of your environment.