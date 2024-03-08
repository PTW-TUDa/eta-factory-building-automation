ETA Factory Building Automation
===============================

The *ETA Factory Building Automation (ETA BA)*, developed by the team of the `ETA-Fabrik <https://www.ptw.tu-darmstadt.de>`_ at Technical University of Darmstadt, is a TwinCat 3 project implementing the default and rule-based control strategy of the thermal supply systems at ETA Research factory.

Project structure
-----------------

The library *ETA BA* consists of three packages for DUTs, POUs and GVLs (not pubslished for safety reasons as they hold safety-critical information). DUT and POU package are structured in the following subpackages:

- The **01 Actors** package holds implementations for single components (e.g. energy converters and storages).
- The **02 Sensors** package holds implementations for complex and unique sensor components (e.g. weather station, measurements from low voltage main distribution).
- The **03 Storages** package holds implementations for active and passive thermal storages consisting of multiple sensors.
- The **04 Systems** package holds implementations for systems consising of a main components (e.g. energy converter) and sub-components (e.g. pumps, valves and sensors).
- The **05 Thermal Netoworks** package holds implementations of thermal networks operating at different temperature levels consisting of multiple systems (from **04 Systems**).
- The **06 Strategy** package holds implementations of the supervisory control strategy for the thermal networks and their sub-systems. Here, we implement for example dynamic flow temperature control, converter sequencing control and thermal storage control rules.
- The **07 Utilities** package holds some utility functions.

Citing this project
--------------------

Please cite this project using our publication:

.. code-block::

    Frank, Michael; Borst, Fabian; Theisinger, Lukas; Lademann, Tobias; Weigold, Matthias (2023): 
    Framework for Rapid Implementation of Building Automation Control Programs of Industrial Heating and Cooling Systems. 
    In: Unpublished, Vol: Unpublished, pp. Unpublished, Unpublished, DOI: Unpublished
