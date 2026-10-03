# ChipWhisperer Studio MCP tool reference

Generated from the MCP server of chipwhisperer-studio 0.5.1 by tools/skill_reference.py (106 tools). Do not edit by hand.

Each entry: `name(arguments)` with defaults (`?` means optional with no default), whether it changes anything, then the server's own description. Every result arrives wrapped as `{"result": ...}`.

## General

- `studio_status()` [read only]: Current state: connected scope and target, running job and its progress, stored trace count, CPA summary, data folder and the Studio URL.
- `studio_options()` [read only]: Every choice Studio accepts: scope kinds, target kinds, programmers, CPA leakage models, crypto targets, SimpleSerial versions, host platform and version.
- `list_devices()` [read only]: List ChipWhisperer USB devices attached to the machine running Studio (name, serial number, whether in use).
- `get_logs(since_seq=0, limit=200, level=None)` [read only]: Recent Studio and ChipWhisperer log messages (also capture and glitch events), optionally only those after sequence number since_seq or at a given level.
- `open_ui(open_browser=False)` [read only]: Return the URL of the Studio web UI (live waveform, settings tree) and optionally open it in the default browser on this machine.

## Scope

- `scope_connect(kind='auto', sn=None, force=False, default_setup=True, sim_model=None)` [changes state]: Connect to a capture scope. kind='sim' uses the built-in simulator (no hardware); sim_model picks which ChipWhisperer it stands in for (default husky), which decides the protocols, triggers and programmers offered. sn picks a device by serial number, force reconnects a device another program left open, default_setup applies ChipWhisperer's recommended settings for the default target.
- `scope_disconnect()` [changes state]: Disconnect the scope (and release the USB device).
- `scope_get_settings(filter=None, include_docs=False)` [read only]: All scope settings as a flat list of {path, value, type, writable, choices} read back from the hardware; filter keeps paths containing the text (e.g. 'glitch', 'adc', 'clock'); include_docs adds each setting's documentation.
- `scope_set_setting(path, value)` [changes state]: Set one scope setting by dotted path (e.g. 'gain.db'=25, 'adc.samples'=5000, 'adc.offset'=0, 'trigger.triggers'='tio4', 'clock.clkgen_freq'=7.37e6, 'glitch.width'=10). Returns the value read back from the hardware.
- `scope_set_settings(settings)` [changes state]: Set several scope settings in order, given as {path: value}; stops at the first error and reports what was applied.
- `scope_action(action)` [changes state]: Run a scope action: default_setup (recommended defaults), arm_capture (arm and wait for one trigger without talking to the target, to test triggering) or reset_fpga.

## Target and serial

- `target_connect(kind='SimpleSerial2', options=None)` [changes state]: Connect the target interface. SimpleSerial2 matches current ChipWhisperer firmware, SimpleSerial is the legacy v1 protocol, sim is the simulated AES target. options are passed to chipwhisperer.target() (e.g. {'bitfile': ...} for CW305).
- `target_disconnect()` [changes state]: Disconnect the target interface.
- `target_get_settings(filter=None, include_docs=False)` [read only]: All target interface settings (baud rate, protocol options...) as a flat list, same format as scope_get_settings.
- `target_set_setting(path, value)` [changes state]: Set one target interface setting by dotted path (e.g. 'baud'=38400).
- `target_program(path, programmer='STM32F')` [DESTRUCTIVE]: Erase and program the target microcontroller with a .hex/.bin file on the machine running Studio, using the chosen programmer (STM32F for CWLITEARM/Nano/STM32 targets, XMEGA for CWLITEXMEGA, AVR for ATmega, SAM4S for Husky's target, NEORV32 for the soft core), or load an FPGA bitstream (iCE40, XC7A35T on CW312T boards). Programmers the connected model does not support are refused with the reason (see hardware_capabilities).
- `serial_write(data, hex=False, newline=True, eol=None)` [changes state]: Write raw data to the target serial port: text, or hex bytes when hex=true; newline appends a line feed, or eol picks the line ending (none, lf, cr, crlf).
- `serial_read(since=0, limit=200)` [read only]: Serial traffic seen by Studio (both directions) after timestamp 'since', each entry {t, dir, data, hex}.
- `simpleserial(cmd='p', data='', read_cmd='r', read_len=None)` [changes state]: Send one SimpleSerial command with a hex payload and read the reply, e.g. cmd='k' with a 16 byte key, cmd='p' with a plaintext (read_len=16 for AES output). read_len=0 skips reading.
- `simpleserial_connect(version='2.1')` [changes state]: Connect the target with a SimpleSerial protocol version: 1.0 (no acks), 1.1, 2.1 (current firmware) or cdc (v2 over the scope's USB-CDC port, when the firmware has it). Then use simpleserial or serial_write.

## Capture

- `capture_start(count=100, mode='simpleserial', key_mode='fixed', text_mode='random', key=None, text=None, length=16, seed=None, store=True, clear=False, ack=True, max_timeouts=10, max_rate=0, wait=True, timeout_s=600)` [changes state]: Capture power traces. count=0 runs until capture_stop. mode='simpleserial' sends key/plaintext to the target each trace, 'trigger_only' just arms and waits for the target's own trigger. key_mode/text_mode choose fixed, random or counter data (key/text give fixed values in hex, length is the block size, seed makes random data repeatable). store=false only displays; clear empties the trace store first; ack waits for the SimpleSerial ack; max_timeouts aborts after that many consecutive timeouts; max_rate limits traces/s (0 = unlimited). wait=true returns when the capture ends.
- `capture_single(key=None, text=None, mode='simpleserial', store=True, max_points=1000)` [changes state]: Capture exactly one trace and return it (summary statistics plus at most max_points samples).
- `capture_stop()` [changes state]: Stop the running hardware job (capture or glitch sweep).
- `job_wait(timeout_s=60)` [read only]: Wait until the running capture or glitch job finishes (or the timeout passes) and return the final status.

## Traces

- `traces_summary()` [read only]: How many traces are stored, samples per trace and memory used.
- `trace_get(index, max_points=1000)` [read only]: One stored trace: its plaintext, ciphertext and key (hex), summary statistics (min, max, mean, std, argmin, argmax) and at most max_points samples (min/max decimated for long traces).
- `traces_stats(start=0, end=None, max_points=1000)` [read only]: Per-sample statistics over stored traces start..end (mean, min, max, std...) each decimated to at most max_points, useful to find where the target is computing.
- `traces_clear()` [DESTRUCTIVE]: Delete all stored traces from memory (exports on disk are kept).
- `traces_export(path='traces', format='npz')` [changes state]: Save stored traces with their inputs/outputs/keys: npz (numpy), cwp (ChipWhisperer project, opens in the Python API) or csv. Relative paths go into Studio's data folder.
- `traces_import(path, replace=True)` [changes state]: Load traces from an .npz or ChipWhisperer .cwp file on the machine running Studio; replace=false appends to the stored traces.

## CPA

- `cpa_start(model='sbox_hw', trace_start=0, trace_end=None, point_start=None, point_end=None, bytes=None, known_key=None, use_stored_key=True, report_every=50, wait=True, timeout_s=600)` [changes state]: Run a correlation power analysis attack on the stored traces. model is the leakage model (sbox_hw for software AES such as TINYAES128C, lastround_hd for hardware AES). trace_start/trace_end and point_start/point_end limit traces and sample window; bytes limits key bytes (0-15); known_key (hex) or the stored key enables partial guessing entropy; report_every sets progress granularity. wait=true returns the final result.
- `cpa_stop()` [changes state]: Stop a running CPA attack (the partial result stays available).
- `cpa_result()` [read only]: Latest CPA result: best key guess, per-byte ranking with correlation values, PGE per byte when the key is known, progress.
- `cpa_correlation(byte, max_points=1000)` [read only]: Correlation versus sample index for the best guess of one key byte (0-15), decimated, which shows where in the trace the leakage happens.

## Glitch

- `glitch_start(parameters, command='g', data='', expected=None, output_len=4, repeats=1, order='nested', reset='nrst', reset_on='reset', reset_delay=0.05, glitch_timeout=1000, arm_scope=True, wait=False, timeout_s=1800)` [changes state]: Sweep glitch parameters. parameters is a list of {path, start, stop, step} or {path, values:[...]} over scope settings (e.g. glitch.width, glitch.offset, glitch.ext_offset, glitch.repeat); every combination is tried 'repeats' times in nested or random order. Each point sends SimpleSerial command with hex data and reads output_len bytes; a valid reply different from expected (hex) counts as success, no reply as reset. reset chooses how to recover the target (nrst pin, pdic, none), reset_on when (never, after a reset result, always), reset_delay in seconds. Configure glitch.clk_src/output/trigger_src first with scope_set_setting.
- `glitch_results(only=None, limit=500)` [read only]: Results of the current or last glitch sweep: counts per outcome and each point's parameter values, outcome and response; only filters by outcome.
- `glitch_export(path='glitch_results.csv')` [changes state]: Save the glitch sweep results as CSV (relative paths go into Studio's data folder).

## Toolchains

- `toolchains_list()` [read only]: Compiler toolchains Studio knows (GCC for Arm, AVR and RISC-V, clang, make for Windows, plus custom ones): version, download size, whether installed, bin folder, a system copy on PATH, and any running download.
- `toolchain_install(toolchain_id, wait=True, timeout_s=1800)` [network]: Download, verify (SHA-256) and unpack a toolchain by id (arm-gcc, avr-gcc, riscv-gcc, clang, win-build-tools or a custom id). Works offline afterwards.
- `toolchain_cancel(toolchain_id)` [changes state]: Cancel a running toolchain download.
- `toolchain_remove(toolchain_id)` [DESTRUCTIVE]: Delete an installed toolchain from disk (it can be installed again later).
- `toolchain_add_custom(name, compiler='gcc', arch=None, prefix='', url=None, sha256=None, path=None, version=None, bin='bin')` [changes state]: Register another toolchain, e.g. for targets Studio has no download for (TriCore, PowerPC, RX) or a specific GCC version: give an archive url (+ sha256) to download, or path to an existing install. arch lists the architectures it serves (arm, avr, riscv, tricore, ppc, rx...), prefix is the tool prefix (e.g. 'arm-none-eabi-').
- `toolchain_remove_custom(toolchain_id)` [DESTRUCTIVE]: Remove a custom toolchain entry (and its download, if Studio installed it).
- `toolchains_refresh()` [network]: Fetch the latest toolchain list published in the Studio repository, so newer compiler versions become available without updating Studio.

## Firmware

- `firmware_catalogue()` [read only]: Firmware sources state (folder, upstream repo, channel, installed commit, update check) plus buildable projects (simpleserial-aes, simpleserial-glitch...), every platform with its architecture, MCU and programmer, crypto targets and SimpleSerial versions.
- `firmware_set_channel(channel='develop')` [changes state]: Choose which upstream newaetech/chipwhisperer version firmware sources follow: a branch (develop), 'latest-release', or any tag or commit. Apply it with firmware_fetch_sources.
- `firmware_check_updates()` [network]: Ask GitHub for the newest commit on the chosen channel and report whether the downloaded firmware sources are out of date.
- `firmware_fetch_sources(ref=None, wait=True, timeout_s=900)` [network]: Download or update the ChipWhisperer firmware sources (firmware/mcu and the fw-extra HALs) from newaetech's GitHub at the channel's current commit, or at ref (branch, tag or commit) if given.
- `firmware_set_folder(root=None)` [changes state]: Build from your own firmware folder (firmware/mcu of a ChipWhisperer checkout, e.g. with local changes) instead of the downloaded sources; root=None goes back to the downloaded sources.
- `firmware_plan(project='simpleserial-aes', platform='CWLITEARM', compiler='gcc', crypto_target=None, ss_ver=None, cflags=None, make_args=None)` [read only]: Show exactly how a build would run (make command, toolchains chosen, output file, programmer) without building.
- `firmware_build(project='simpleserial-aes', platform='CWLITEARM', compiler='gcc', crypto_target='TINYAES128C', ss_ver='SS_VER_2_1', cflags=None, make_args=None, clean=True, jobs=None, wait=True, timeout_s=900, log_tail=40)` [changes state]: Compile a ChipWhisperer firmware project for a platform with GCC or clang using ChipWhisperer's makefiles. crypto_target picks the AES/crypto implementation (TINYAES128C, AVRCRYPTOLIB, MBEDTLS, HWAES...), ss_ver the SimpleSerial protocol (SS_VER_2_1 for current Studio/ChipWhisperer, SS_VER_1_1 legacy), cflags adds compiler flags, make_args adds make variables (e.g. 'OPT=2 EXTRA_OPTS=...'), clean rebuilds from scratch. Returns the .hex path, sizes and programmer, plus the end of the build log.
- `firmware_build_log(since=0, limit=400)` [read only]: Build output lines after line number 'since' (use 'next' from the reply to continue).
- `firmware_build_cancel()` [changes state]: Stop a running firmware build.
- `firmware_builds()` [read only]: Firmware images built so far (newest first) with their paths, ready for firmware_program or target_program.
- `firmware_program(path=None, programmer=None)` [DESTRUCTIVE]: Program the target with the last successful build (or path) using the platform's programmer (or the one given). Needs a connected scope.

## Notebooks and kernels

- `notebook_run_code(code, notebook_path=None, timeout_s=600)` [changes state]: Run Python in a Studio notebook kernel and return its output. Without notebook_path it runs in the shared default kernel (one persistent namespace for agent code); with notebook_path (relative to the notebooks folder) it runs in that notebook's own kernel, the namespace its tab in the Notebook tab uses, with the notebook's folder as working directory. Inside, `import chipwhisperer as cw` gives cw.scope()/cw.target() bound to Studio's connected devices, cw.capture_trace() stores traces in the Capture tab, IPython magics and !shell commands work, and `studio` offers studio.traces, studio.add_trace(), studio.build_firmware(), studio.program(). Cells of all kernels run one at a time.
- `notebook_list()` [read only]: Notebooks stored in Studio (paths relative to the notebooks folder) and the state of the ChipWhisperer tutorial download.
- `notebook_read(path, include_outputs=True)` [read only]: A notebook's cells (type, source and, optionally, a text summary of the outputs).
- `notebook_write(path, cells)` [changes state]: Create or overwrite a notebook from a list of cells, each {"type": "code" or "markdown", "source": "..."}; the user sees it in the Notebook tab.
- `notebook_run(path, stop_on_error=True, timeout_s=1800)` [changes state]: Run every code cell of a stored notebook in order (like Run all) in that notebook's own kernel, save the outputs into the notebook and return a per-cell summary. Its variables stay available to notebook_run_code(notebook_path=path) until kernel_shutdown.
- `kernel_variables(notebook_path=None)` [read only]: Variables defined in a notebook kernel (name, type, shape, short repr): the shared default kernel, or the kernel of the notebook at notebook_path.
- `kernel_list()` [read only]: Running notebook kernels: id (the notebook path, or "default"), busy, queued cells, execution count, number of variables and how many Studio windows have the notebook open.
- `kernel_interrupt(notebook_path=None)` [changes state]: Interrupt the running cell of a kernel and drop its queued cells: the shared default kernel, the kernel of the notebook at notebook_path, or every kernel with notebook_path="all".
- `kernel_restart(notebook_path=None)` [DESTRUCTIVE]: Restart a kernel, clearing its variables (Studio's hardware connection is kept): the shared default kernel, or the kernel of the notebook at notebook_path. Other notebooks are not affected.
- `kernel_shutdown(notebook_path)` [DESTRUCTIVE]: Shut down the kernel of the notebook at notebook_path and free its memory (it starts again, empty, when the notebook next runs a cell). Use it after notebook_run on notebooks you no longer need.
- `tutorials_fetch(wait=True, timeout_s=900, on_modified=None)` [network]: Download NewAE's tutorial notebooks (chipwhisperer-jupyter, matched to the installed firmware sources) into the notebooks folder, with firmware paths linked so their build cells work. Locally edited or added files are kept; if some of them would be replaced by a different upstream version the job stops in state "confirm" listing job.conflicts, and nothing changes until you call again with on_modified="backup" (install the new versions and keep the local copies as <name>.local-<timestamp>.<ext>) or on_modified="keep" (leave the local copies in place). Ask the user before choosing.

## Notes and calculator

- `notes_list()` [read only]: Text notes in Studio's notes pad.
- `note_read(name)` [read only]: Read one note.
- `note_write(name, text, append=True)` [changes state]: Write to a note (created if missing); append=true adds the text at the end, for example to record a recovered key or working glitch settings.
- `calculate(expression)` [read only]: Evaluate a calculator expression: arithmetic, bitwise (^ is XOR), hex/bin, math functions, mean/median/std/rms, and side-channel helpers hw(x), hd(a, b), sbox(x). Variables persist (x = 3), ans is the last result.
- `selection_stats(values=None, trace_index=None, start=None, end=None, sample=None)` [read only]: Statistics (count, sum, mean, median, min, max, peak to peak, std, variance, RMS) of explicit values, of samples start..end of a stored trace (trace_index, -1 = newest), or of one sample index across all stored traces.

## Interfaces

- `hardware_capabilities()` [read only]: What the connected ChipWhisperer (or the simulator's model) supports: UART pin modes, SimpleSerial versions, SPI, JTAG/SWD, GPIO, USERIO, bit-banger, 1-Wire, each trigger type and each programmer, as {available, reason, details}. Check this before using the other interface tools.
- `interfaces_status()` [read only]: State of every interface: capabilities, UART configuration and pins, SimpleSerial version, SPI master, the last trigger configured here, OpenOCD server and MPSSE mode.
- `uart_configure(baud=None, parity=None, stop_bits=None, rx=None, tx=None)` [changes state]: Configure the target UART: baud (500 to 2000000), parity, stop bits (1, 1.5 or 2; data bits are always 8) and which TIO pins are RX and TX. Only pin modes valid for the connected model are accepted (the CW-Nano is fixed to RX on TIO1 and TX on TIO2; TIO4 can only transmit).
- `spi_enable(speed=1000000.0, cs='pdid')` [changes state]: Turn on the SPI master on the 20-pin header (SCK, MOSI, MISO; chip select on the chosen pin). Needs the TARGET_SPI firmware feature; not on the CW-Nano. nRST is held high while SPI is on. On the simulator a W25Q128-style SPI flash (JEDEC ID EF 40 18) answers.
- `spi_disable()` [changes state]: Turn off the SPI master and release its pins.
- `spi_transfer(data, start=True, stop=True)` [changes state]: Send hex bytes on MOSI (e.g. '9f 00 00 00' reads a flash JEDEC ID) and return the bytes read on MISO. start/stop control chip select at the beginning and end, so a long transaction can be split.
- `gpio_read()` [read only]: Drive mode and logic level of each target pin (TIO1-4, nRST, PDIC, PDID, and on the Husky MISO/MOSI/SCK). The CW-Nano can drive pins but not read them back.
- `gpio_set(pin, state)` [changes state]: Drive a target pin high or low, or release it (high_z). Pins the model cannot drive are refused with the reason.
- `gpio_pulse(pin='nrst', ms=50)` [changes state]: Pull a pin low for ms milliseconds and release it; on nRST this resets the target.
- `userio_set(direction=None, drive=None)` [changes state]: Husky USERIO header: direction is a bit mask (bit n = 1 drives Dn, bit 8 is CK where the library supports it) and drive the levels to drive. Returns the pins read back. Husky only.
- `trigger_configure(kind='basic', pins=None, op='OR', edge='rising_edge', pin=None, baud=None, pattern=None, rule=0, data_bits=8, stop_bits=1, parity='none', edges=1, level=0.1, window_start=0, window_end=0, threshold=10, start=0, enabled=True)` [changes state]: Configure the capture trigger; captures use it right away. basic: pins combined with op, edge or level mode (CW-Nano: TIO4 rising edge only). uart_decode (Pro): UART byte pattern on pin at baud, pattern as quoted text "'r'" or hex '72 XX' (XX = any byte, up to 8). uart_pattern (Husky): pattern match rule (2 rules on Husky, 8 on Husky Plus) with data_bits/stop_bits/parity. edge_counter (Husky): trigger after edges edges on pin. adc_level (Husky): trigger when the signal crosses level (-0.5..0.5). sequencer (Husky): two pins in order within window_start..window_end ADC cycles, enabled=false turns it off. sad (Pro, Husky): sum of absolute differences against the newest trace from sample start, threshold.
- `bitbang(bits, record=None, data_pin='USERIO_D0', clock_pin='USERIO_CK', clk_div=None)` [changes state]: Husky bit-banger: send bits ('0' and '1') on data_pin with a clock on clock_pin ('disabled' for none); record has one 0/1 per bit, 1 = release the line and record it. clk_div (even, from the ADC clock) sets the time slot. Returns the recorded bits. Needs a Husky and a chipwhisperer library newer than 6.0.0.
- `onewire(action='read_rom', data_pin='USERIO_D0', clk_div=None)` [changes state]: 1-Wire on the Husky bit-banger: reset (reset pulse and presence detect) or read_rom (command 0x33: family code, 48-bit serial, CRC check). The simulator has a DS18B20-style device (family 0x28).

## OpenOCD

- `openocd_status(list_targets=False, filter=None)` [read only]: OpenOCD state: whether it is installed (toolchain id 'openocd', install with toolchain_install), the server and its GDB/telnet/TCL ports, MPSSE mode, recent log lines; list_targets adds the target configs from OpenOCD's scripts folder (filter by text, e.g. 'stm32').
- `openocd_mpsse(enable=True, transport='jtag', header='target')` [changes state]: Switch the scope into MPSSE mode for JTAG or SWD (header 'userio' routes it to the Husky USERIO header) or back to normal mode (enable=false, which reconnects the scope; it also restores a scope Studio found already in MPSSE mode, for example after a restart, shown as mpsse.detected in openocd_status). While MPSSE is on, Studio has no scope connection, USB-CDC serial and the native programmers are unavailable. Not on the simulator.
- `openocd_start(target_cfg='target/stm32f3x.cfg', transport=None, gdb_port=3333, telnet_port=4444, tcl_port=6666)` [changes state]: Start an OpenOCD server with the ChipWhisperer interface config and a target config from OpenOCD's scripts folder (see openocd_status list_targets). Needs MPSSE mode (openocd_mpsse) and real hardware. GDB connects to gdb_port.
- `openocd_stop()` [changes state]: Stop the OpenOCD server.
- `openocd_command(command)` [changes state]: Run an OpenOCD command on the running server (e.g. 'targets', 'halt', 'reg', 'mdw 0x08000000 4', 'reset run') and return ok and its output.
- `openocd_program(path, verify=True, reset=True, address=None)` [DESTRUCTIVE]: Flash a .hex/.elf (or .bin with address, e.g. '0x08000000') on the Studio machine through OpenOCD's program command, then verify and reset.

## Logic analyser

- `la_sources()` [read only]: Logic analyser sources and whether each is available (with the reason when not): native (Husky scope.LA: 9-signal groups 'CW 20-pin', 'USERIO 20-pin', 'glitch'), adc (one line on the analog input of any ChipWhisperer, thresholded), sim (the simulator's demo traffic: UART, SPI, I2C, 1-Wire, CAN, JTAG, SWD, trigger, clock), sigrok (external analysers through sigrok-cli, with install steps when missing) and files (VCD, CSV, .sr). Also lists LA triggers, clock sources and the decoders with their options.
- `la_capture(source='sim', group=None, clk_source=None, oversampling=None, downsample=None, depth=None, trigger=None, with_analog=None, fire=None, segments=None, samples=None, level=None, hysteresis=None, sim_signal=None, samplerate=None, duration_ms=None, pretrigger=None, channels=None, jitter_ns=None, glitches_per_ms=None, device=None, time_ms=None, triggers=None, timeout=None)` [changes state]: Capture logic data and wait for it; returns the capture summary (channels, sample rate, trigger index, duration). native (Husky): group, clk_source and oversampling set the sampling clock (rate = source x oversampling / downsample), depth (max 16376 Husky, 65535 Husky Plus), trigger ('capture', 'manual', 'rising_tio1', 'falling_userio_d3', 'HS1', 'glitch', ...; no pre-trigger), fire='simpleserial' sends a command so the target raises its trigger, with_analog also keeps the ADC trace (trigger 'capture'). adc (any ChipWhisperer): segments traces of samples each, level ('auto' or volts) and hysteresis; on the simulator sim_signal picks the demo line on the measure input. sim: samplerate, duration_ms, pretrigger (%), channels, jitter_ns, glitches_per_ms. sigrok: device (e.g. 'fx2lafw' or 'demo'), samplerate, channels, samples or time_ms, triggers ('D0=r,D1=1'). Defaults match the Logic tab (native downsample 96, adc segments 20). timeout is how long the analyser waits for its trigger (native default 5 s, sigrok default 60 s); the tool waits that long plus a margin for the capture to finish.
- `la_import(path, format=None, samplerate=None)` [changes state]: Import a logic capture from any analyser: a .vcd, .csv (Saleae Logic export, or a time/sample-index column plus one 0/1 column per channel) or sigrok .sr file on the Studio machine. samplerate is needed for CSV files with sample indices or no time column. It becomes the current capture.
- `la_export(format='vcd', path=None, channels=None)` [changes state]: Write the current capture as VCD, CSV (Saleae style) or a sigrok .sr session; path defaults to Studio's logic/exports folder. channels exports a subset (names or indices).
- `la_decode(decoder, channels=None, options=None, limit=200, add_to_view=False)` [changes state]: Decode the current capture and return the annotations (time from the trigger, row, channel, kind, text, value). channels maps roles to channel names or indices (uart/simpleserial: rx, tx; spi: cs, sck, mosi, miso; i2c: scl, sda; onewire: owr; jtag: tck, tms, tdi, tdo; swd: swclk, swdio; can: can); left out, they are guessed from the channel names. options: uart baud ('auto' or a number), data_bits 5-9, parity, stop_bits, bit_order, inverted; spi mode 0-3, bit_order, word_size, cs_active; i2c address_format; can bitrate, sample_point; simpleserial version auto/1/2 plus the UART options; sigrok spec (e.g. 'uart:rx=D0:baudrate=115200', channels named D0..Dn by position, needs sigrok-cli). add_to_view also shows it in the Logic tab.
- `la_measure(channel=None, t0=None, t1=None, from_sample=None, to_sample=None)` [read only]: Measure a channel (name or index; all channels when left out) of the current capture between t0 and t1 seconds from the trigger (or sample indices): edge counts (rising/falling), frequency, period, duty cycle and high/low pulse width min/max/avg.
- `la_channels(rename=None, hide=None, show=None, order=None, colors=None, buses=None)` [changes state]: List the current capture's channels (index, name, colour, hidden, edge count) and change them: rename {old: new}, hide/show lists, order (top to bottom), colors {name: '#rrggbb'}, buses [{name, channels (MSB first), format hex/dec/bin}] shown as a value row. With no arguments it only lists.
- `la_search(kind='edge', channel=None, edge='any', pattern=None, edge_channel=None, text=None, from_sample=-1, direction='next')` [read only]: Find the next (or previous) edge on a channel, a pattern across the shown channels in display order ('1X0': first channel high, second any, third low; optionally only at an edge of edge_channel), or a decoded value (text like '0x41' or 'NACK'), starting from a sample index. Returns the sample index and time.
- `la_status()` [read only]: The logic analyser state: a running capture, the current capture's summary, the captures kept in memory, configured decoders and buses.
- `la_select(capture)` [changes state]: Make another of the captures kept in memory (the newest eight, ids from la_status) the current one, which every other la_* tool works on. Returns its summary.

## Code map

- `code_map_build(elf=None, sources=None, trace=None, key=None, text=None, core=None, protocol=None, wait_states=None, align=None, target_freq=None, adc_freq=None, top=25, cmd=None, raw=None, max_instructions=None)` [changes state]: Work out which firmware code runs when during a captured trace. Emulates the ELF (default: the firmware programmed in this session, else the newest build) for the key and plaintext of stored trace `trace` (default the newest; or give key/text in hex), maps clock cycles to samples from the scope's clocks and ADC settings, and aligns the emulated power model with the stored traces. Supports Arm Cortex-M (Unicorn), RISC-V RV32 (Unicorn) and AVR/XMEGA (Studio's own cycle-accurate emulator). core overrides the timing model (cortex-m0, cortex-m0+, cortex-m3, cortex-m4, cortex-m7, cortex-m33, rv32-generic, ibex, neorv32, avr, avrxmega). sources is a folder to find the source files in. cmd is the SimpleSerial command to emulate (default 'p'; e.g. 'g' for simpleserial-glitch, with text "" for no data); raw is hex serial input fed as it is, for firmware that is not SimpleSerial (the response is then its output); max_instructions raises the per command limit (default 4000000) for long computations. A firmware that ends in an endless loop after answering reports where in `halted`. Returns whether the emulated ciphertext is the correct AES and matches the stored one, the mapping and alignment, and the `top` functions that run in the trigger window with their sample ranges.
- `code_map_region(start, end, limit=100)` [read only]: Which code a range of samples represents (after code_map_build): the functions with their self cycles, share of the range and inclusive spans, and the source lines (file, line, source text, cycles, how often they ran), in the order they first run.
- `code_map_lookup(function=None, file=None, line=None)` [read only]: Where a function (by name) or a source line (file name or path, and line number) ran during the trace: its cycle and sample ranges.
- `code_map_align(source='mean', trace=None, scale_min=0.9, scale_max=1.1, max_shift=None, shift=None, scale=None)` [changes state]: Fit the cycle to sample mapping by cross-correlating the emulated power model with the mean of the stored traces (or one trace): returns the shift (samples), scale, correlation, a confidence (0..1, label high/medium/low: high means a strong correlation with no rival match within the search window) and whether the fit was applied (a low confidence fit is not: the nominal mapping is kept). Giving shift and/or scale instead sets the mapping by hand.
- `code_map_disassemble(function=None, file=None, line=None)` [read only]: Disassembly of a function or of a source line (file and line) of the mapped firmware, with how often each instruction ran in the emulated trace.
- `code_map_pc_trace(interval=64)` [changes state]: Exact mode: record real program counter samples through a ChipWhisperer-Husky's Arm trace port (SWO, every `interval` cycles, rounded to the nearest the Arm DWT supports: 64 or 1024 times 1 to 16, the result's `interval`; needs simpleserial-trace firmware) and compare them with the emulation: agreement (share of samples in the predicted function) and the fitted cycle scale. On the simulator the samples come from the emulated run through the same decoder.

## Prompts

- `cpa_attack(platform, traces, simulate)`: Step by step CPA key recovery on a ChipWhisperer target.
- `glitch_search(parameter_ranges)`: Guided clock glitch parameter search against simpleserial-glitch.

## Resources

- `studio://status`
- `studio://firmware`
