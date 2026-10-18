FROM python:3.12-slim

# Same numeric uid as the agents' user so this container can write into
# the shared mailbox volume (Docker matches uid, not name).
RUN useradd --create-home --shell /bin/bash agent \
 && mkdir -p /data /shared/mailbox \
 && chown -R agent:agent /data \
 && chmod 777 /shared /shared/mailbox

COPY bank.py /app/bank.py
RUN chmod 755 /app/bank.py

USER agent
ENV PYTHONUNBUFFERED=1
EXPOSE 8790
ENTRYPOINT ["python3", "/app/bank.py"]
