"""
inventory_pipeline
==================
Pipeline ETL que consolida el inventario de servidores físicos y máquinas
virtuales (salida de Rundeck + inventario maestro) y lo sincroniza con NetBox.

El punto de entrada recomendado es el orquestador `run_pipeline`
(comando de consola `inventory-pipeline`); cada etapa puede ejecutarse
también por separado con `python -m inventory_pipeline.<módulo>`.
"""
