# ProGuard / R8 rules for the CareVoice app.
#
# Code shrinking is currently disabled (minifyEnabled false), so these rules
# are inert. They are kept as the conventional home for keep-rules if release
# minification is enabled later.

# WebRTC exposes its API via JNI; keep it if minification is ever turned on.
-keep class org.webrtc.** { *; }
