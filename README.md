### Card Printer
Generates Print then Cut read files compatiable with `CARD_TEMPLATE_FOR_GPTv2.pdf` in the Cricut Design Studio
1. Load the template into your circut design space from this url https://design.cricut.com/landing/project-detail/68e7c37c740596b350cdf918
2. Get the card art you want from https://mpcfill.com/
3. Download that art and put it in the `card_art` folder in this project
4. Make the `runScript.sh` executable on your machine `chmod +x ./runScript.sh` 
5. Run `./runScript.sh`
6. Optional: If you have a card back you'd like to use make folder in this repo called `backs` then put that image of the card back in there named `Backs.pdf` and lastly make the script executable `chmod +x ./runAddBacks.sh` then run  `./runAddBacks.sh`