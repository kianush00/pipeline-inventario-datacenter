"""
inventory_pipeline
==================
ETL pipeline that consolidates the inventory of physical servers and virtual
machines (Rundeck output + master inventory) and synchronizes it with NetBox.

The recommended entry point is the `run_pipeline` orchestrator
(`inventory-pipeline` console command); each stage can also be run
separately with `python -m inventory_pipeline.<module>`.
"""
