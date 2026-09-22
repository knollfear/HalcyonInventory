# Invoice samples

Drop a supplier's order confirmation in here as a plain `.txt` (or the PDF /
photo itself) and read it without touching a row:

    docker compose exec web python manage.py read_invoice /home/app/samples/whatever.txt

The repo dir is mounted at `/home/app`, so a file saved here is visible to the
container straight away. Nothing in this folder is committed except this
README — real order paperwork has prices and account numbers on it, and a
sample file is not worth putting in the history to find out.

The same text pasted into the box on `/scarves/private/invoices/` does exactly
the same thing, and then gives you the page to confirm.
