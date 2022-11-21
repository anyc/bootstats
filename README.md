bootstats
=========

bootstats is a utility to measure the time a device requires to reach certain
stages during the boot phase. To get precise measurement results - especially
during early boot stages like the bootloader - bootstats is not run on the
device under test (DUT) but on a separate device and it checks the UART ("serial
console") output for configured string patterns. In addition, a sigrok-supported
probe can be used to get the precise time the board is powered on - e.g., if the
power is enabled over a remote-controlled power outlet which adds inaccuracy
to the measurement results.

If the device that runs bootstats provides network services to the device under
test, bootstats can also monitor the local system log for interesting events
like connection attempts.

Furthermore, user-defined tasks can be started by bootstats to, e.g., measure
when network services on the DUT are processing actual requests. See
`task_echo.py` for an example that sends UDP packets to the device under test
and logs when a response is received.

See also [grabserial](https://github.com/tbird20d/grabserial) if you look for
a similar tool.

Dependencies:
 * pyserial

Optional dependencies:
 * sigrok python bindings
 * systemd python bindings

Examples
--------

Show supported sigrok drivers and available devices:
```
bootstats.py  --sr_scan
```

Look for string `U-Boot` thirty times and show the average time under the Id
"U-Boot start":
```
bootstats.py  --iterations 30 --trigger "U-Boot start:U-Boot"
```

If you need a more complex setup, a configure file `bootstats.cfg` like the
following can be created:

```
[general]
# use sigrok channel D7 to wait on power events
sr_channels=D7
# execute the following commands to power on/off the device
poweron=power_toggle.sh 1
poweroff=power_toggle.sh 0

# measure time when "U-Boot SPL" is read and store it under Id "SPL"
[trigger_SPL]
trigger=U-Boot SPL

[trigger_Uboot]
trigger=U-Boot 20

[trigger_StartKernel]
trigger=Linux version

[trigger_OSWelcome]
trigger=Welcome to
# power-cycle the device after "Welcome to" was read
powerCycle=1
```

The last part of the resulting output could look like this:
```
        power_on   0.026981   0.006456
             SPL   0.062087   0.006563
           Uboot   0.092087   0.006213
     StartKernel   4.567868   0.005712
       OSWelcome  12.590905   0.161515
```
