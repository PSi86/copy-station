Copy_Station Project

Goal: Automatically copy contents of a camera with integrated memory (DJI O4 Air Unit (non-pro) or similar) to a (Micro-) SD-Card.

System Components:
Radxa Cubie A7S (https://radxa.com/products/cubie/a7s/)
Anker 341 USB-C Netzteil (7-in-1) (https://www.anker.com/eu-de/products/a8346)

Technical Details: 
- The Anker USB Hub is connected to the USB 3.2 Port on the Radxa Cubie A7S. 
- To the Hub the O4 is connected and a sd card is inserted into its card reader.
- A custom system service on the Cubie will auto-transfer the content from the bigger to the smaller mass-storage, check for sucessfull transmission and then delete the content from the source device.
- The source storage will never be formatted - only the subfolder with the media in it should be deleted ("DCIM")
- No files on the target device shall be overwritten - one folder appropriately named per transfer
- The status of the copy station needs to be visualized (led, beeper, etc). Status: Ready, Detecting, Copying, Error.
- The Cubie runs the radxa-a733-bullseye-cli-r6 OS (no graphical desktop environment, only shell)
- The OS is installed on a microSD card inserted into the Cubie
- Cubie is powered by a USB-C powerbank

To develop here: System Service with copy logic, autostart, error handling, information output.