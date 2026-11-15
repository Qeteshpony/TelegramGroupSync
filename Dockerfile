FROM python:3.13-alpine
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt
COPY *.py /bot/
# run as an unprivileged user, /data holds the state file
RUN adduser -D bot && mkdir /data && chown bot /data
USER bot
ENV STATE_FILE=/data/state.json
VOLUME /data
CMD ["python3", "-u", "/bot/groupsync.py"]
