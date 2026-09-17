"""Conftest raíz: agrega la raíz del proyecto al sys.path para imports."""

import sys
from pathlib import Path

# Permite importar los módulos del proyecto directamente (sin instalar
# como paquete) desde cualquier subdirectorio de tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
