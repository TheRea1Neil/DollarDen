# ML Kit text recognition ships one artifact per script. We depend on the
# Latin one only (com.google.mlkit:text-recognition), because receipts here
# are English and each extra script adds its own model to the APK.
#
# The plugin's Java names all five recognisers in one initialize() method,
# so R8 sees unresolved references to the four we left out and fails the
# release build outright:
#
#   Missing class com.google.mlkit.vision.text.korean.KoreanTextRecognizerOptions
#   > Execution failed for task ':app:minifyReleaseWithR8'
#
# Debug builds do not run R8, which is why `flutter build apk` (debug)
# succeeded and only `--release` broke.
#
# Listed per script rather than as a blanket -dontwarn on
# com.google.mlkit.** so that a genuinely missing ML Kit class still fails
# the build instead of being silently waved through.
-dontwarn com.google.mlkit.vision.text.chinese.**
-dontwarn com.google.mlkit.vision.text.devanagari.**
-dontwarn com.google.mlkit.vision.text.japanese.**
-dontwarn com.google.mlkit.vision.text.korean.**
