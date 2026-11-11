# dwsim-dexpi

Converts a [DEXPI](https://dexpi.org/) model into the reading that
[DWSIM](https://dwsim.org)'s *Import Flowsheet from Drawing* tool opens, so a flowsheet can be
built from a DEXPI file without the AI Assistant.

It reads DEXPI 2.0 and 2.0.1 (XML and JSON) and Proteus XML (DEXPI 1.3) through
[pyDEXPI](https://github.com/process-intelligence-research/pyDEXPI).

| DEXPI model | Reading | What is taken |
| --- | --- | --- |
| Process model | PFD | process steps as equipment with what they give for themselves (duty, temperature, pressure, efficiency, pressure ratio, reflux ratio, area, volume, trays), streams with temperature, pressure, mass flow and composition (mass or mole), the vapour fraction from the phases of the material state, the control loops (the measuring and controlling activities and the step they act on), the other names of the compounds (CAS number, IUPAC and ChEBI identifiers), the roles of the ports (top and bottom products, vapour, liquid), the diagram's layout |
| Plant model (P&ID) | P&ID | equipment, control and safety valves, the piping between them with line numbers, the height of the nozzles on the equipment, the design data of the equipment (exchanger duty and area, pump head and power, tank volume), the design pressure and temperature limits, the set pressure of the relief valves, the control loops (what each one measures and the valve it moves), the diagram's layout |

Block valves, check valves, reducers and other in-line pieces are passed through, dead-end
branches (drains, instrument taps) are left out and tees become mixers or splitters. The diagram
is rendered as the picture the import shows the items on.

## Several files

```bash
dwsim-dexpi pid-sheet-1.xml pid-sheet-2.xml process.xml -o plant.reading.json
```

Several files are read as one plant. The sheets of a P&ID are joined where a line leaves one at an
off-page connector and enters another: the connectors are paired by the reference one makes to the
other's number, else by a number they share, else by the line number of the lines they end. A
process model of the same plant gives its stream conditions, compositions and equipment data to the
items and lines of the P&ID with the same tags; junctions, which do not have tags on a P&ID, are
matched by the items they join.

## Install for DWSIM

Download the package for your system from the
[latest release](https://github.com/DanWBR/dwsim-dexpi/releases/latest) and unpack it. DWSIM
looks for the `dwsim-dexpi` program in:

1. the folder named by the `DWSIM_DEXPI_HOME` environment variable;
2. `extenders/dexpi` under DWSIM's folder;
3. the folders of the `PATH`.

Then choose a DEXPI `.xml` file in *CAD Tools > Import Flowsheet from Drawing*.

## Use on its own

```bash
dwsim-dexpi model.xml -o model.reading.json
```

The reading is a JSON file that DWSIM's import opens with *Open a reading*, and that
`CadImport.ImportFromDrawing` takes in scripts. Values are written in °C, bar (absolute) and kg/h.

From the sources, with Python 3.12 or newer:

```bash
pip install -r requirements.txt
python dwsim_dexpi.py model.xml -o model.reading.json
```

## Build

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --name dwsim-dexpi --collect-all pydexpi --collect-all resvg dwsim_dexpi.py
```

The workflow in `.github/workflows/build.yml` builds the Windows, Linux and macOS packages and
attaches them to the release of a `v*` tag.

## License

dwsim-dexpi is licensed under the GNU Affero General Public License v3 or later (see `LICENSE`),
the license of pyDEXPI, which the released programs contain. It is a separate program: DWSIM only
runs it and reads the file it writes.

`tests/synthetic_process.xml` is the synthetic DEXPI 2.0.1 process model of the pyDEXPI
repository (`data/dexpi_2_0_1`), unchanged.
