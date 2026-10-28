#!/bin/sh
# Keep yt-dlp fresh on container start — sites (Instagram, Facebook, etc.) break
# its extractors frequently, and the usual fix is simply updating yt-dlp.
# The curl-cffi extra provides browser impersonation; without it Instagram
# answers anonymous requests with "Instagram sent an empty media response".
# Installs into the reclip user's ~/.local (first on PATH). Skip with RECLIP_NO_UPDATE=1.
if [ -z "$RECLIP_NO_UPDATE" ]; then
    echo "Updating yt-dlp..."
    pip install --user --no-cache-dir -q -U "yt-dlp[default,curl-cffi]" || \
        echo "  (couldn't update yt-dlp — continuing with the installed version)"
fi

exec "$@"
