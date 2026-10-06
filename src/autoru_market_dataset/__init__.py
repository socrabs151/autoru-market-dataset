"""Сборщик объявлений auto.ru.

Ядро не знает, что такое auto.ru: работа с площадкой живёт в
``autoru_market_dataset.frontends``, здесь только контракт и доменные объекты.
"""

__version__ = "0.0.0"

SOURCE = "auto_ru"

__all__ = ["SOURCE", "__version__"]
