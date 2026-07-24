"""Copy_Station -- automatic copy station camera -> SD card.

An event-driven daemon that transfers the footage of a camera connected via USB
as mass storage (DCIM folder) onto an SD card, verifies the transfer and then
clears the source.
"""

__version__ = "1.1.0"
