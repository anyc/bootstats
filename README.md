bootstats
=========

bootstats is a utility to measure the time a device requires to reach certain
stages during the boot phase. To get precise measurement results - especially
during early boot stages like the bootloader - bootstats is not run on the
to-be-analyzed device but on a separate device and it checks the UART output for
configured string patterns. In addition, a sigrok-supported probe can be used to
get the precise time the board is powered on.

See also [grabserial](https://github.com/tbird20d/grabserial) if you look for
a similar tool.

Dependencies:
 * serial_asyncio
 * sigrok python bindings

Examples
--------

Show supported sigrok drivers and available devices:
```
bootstats.py  --sr_scan
```

Look after string `U-Boot` thirty times and show the average time under the Id
"U-Boot start":
```
bootstats.py  --iterations 30 --trigger "U-Boot start:U-Boot"
```

If you need a more complex setup, a configure file `bootstats.cfg` like the
following can be created:

```
[general]
# use sigrok channel D7 to wait on power-on event
sr_channels=D7
# execute the following commands to power on/off the device
poweron=power_toggle.sh 1
poweroff=power_toggle.sh 0

# measure time delta when "U-Boot SPL" is read and store it under Id "SPL"
[trigger_SPL]
trigger=U-Boot SPL

[trigger_Uboot]
trigger=U-Boot 20

[trigger_StartKernel]
trigger=Linux version

[trigger_OSWelcome]
trigger=Welcome to
# power-cycle the device after "Welcome to" is read
powerCycle=1
```

The last part of the resulting output would look like this:
```
Result        power_on   0.026981   0.006456
Result             SPL   0.062087   0.006563
Result           Uboot   0.092087   0.006213
Result     StartKernel   4.567868   0.005712
Result       OSWelcome  12.590905   0.161515
```