"""Logic analyser for ChipWhisperer Studio: captures from any source, file formats, protocol decoders and the queries behind the Logic tab.

* :mod:`cwstudio.logic.model` holds a capture as one sorted transition list per channel, so 10 channels of 10 million samples stay small and every visible-range query is a binary search.
* :mod:`cwstudio.logic.formats` reads and writes VCD, CSV (Saleae and generic) and sigrok ``.sr`` files.
* :mod:`cwstudio.logic.decoders` decodes UART, SPI, I2C, 1-Wire, JTAG, SWD, CAN and SimpleSerial in software.
* :mod:`cwstudio.logic.synth` generates protocol waveforms (for the simulator and for the decoder tests).
* :mod:`cwstudio.logic.sources` captures from the Husky ``scope.LA``, from the analog input of any ChipWhisperer, from the simulator, and from external analysers through :mod:`cwstudio.logic.sigrok`.
* :mod:`cwstudio.logic.service` is the per-session state and the ``/api/la/*`` routes.
"""
from cwstudio.logic.model import AnalogChannel, Channel, LogicCapture  # noqa: F401
