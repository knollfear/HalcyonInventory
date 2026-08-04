FROM python:3.10

WORKDIR /home/app

# libzbar0 is the C library pyzbar wraps to decode barcodes server-side.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libzbar0 \
    && rm -rf /var/lib/apt/lists/*

#If we add the requirements and install dependencies first, docker can use cache if requirements don't change
ADD requirements.txt /home/app
RUN pip install --no-cache-dir -r requirements.txt

ADD . /home/app

EXPOSE 8000