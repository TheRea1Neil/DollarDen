import 'dart:io';

import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';
import 'package:image/image.dart' as img;
import 'package:image_picker/image_picker.dart';

/// Read a receipt photo into text, on the device.
///
/// The vision model was the original plan and does not work on this
/// setup — see ADR 0007. It is also the wrong shape even when it works:
/// OCR cannot invent a line item, and the photo never leaves the phone.
class ReceiptOcr {
  final ImagePicker _picker;
  ReceiptOcr({ImagePicker? picker}) : _picker = picker ?? ImagePicker();

  /// Take or choose a photo. Null if the user backed out.
  Future<File?> pick({required bool fromCamera}) async {
    final shot = await _picker.pickImage(
      source: fromCamera ? ImageSource.camera : ImageSource.gallery,
      // A receipt is text on paper: resolution matters more than
      // colour fidelity, but a 12MP original is far more than the
      // recogniser needs and slows everything down.
      maxWidth: 2200,
      imageQuality: 92,
    );
    return shot == null ? null : File(shot.path);
  }

  /// Extract text, in reading order.
  Future<String> read(File photo) async {
    final prepared = await _prepare(photo);
    final recogniser = TextRecognizer(script: TextRecognitionScript.latin);
    try {
      final result = await recogniser.processImage(InputImage.fromFile(prepared));
      return _inReadingOrder(result);
    } finally {
      await recogniser.close();
    }
  }

  /// Greyscale and stretch the contrast before recognising.
  ///
  /// Till receipts are low-contrast thermal print that fades, and a
  /// photo of one usually has a shadow across it. This costs a few
  /// hundred milliseconds and meaningfully improves what comes back.
  Future<File> _prepare(File photo) async {
    try {
      final decoded = img.decodeImage(await photo.readAsBytes());
      if (decoded == null) return photo;
      final clean = img.adjustColor(
        img.grayscale(decoded),
        contrast: 1.35,
      );
      final out = File('${photo.path}.prepared.jpg');
      await out.writeAsBytes(img.encodeJpg(clean, quality: 92));
      return out;
    } catch (_) {
      // Never let a preprocessing failure stop the actual job.
      return photo;
    }
  }

  /// ML Kit returns blocks in no particular order, and a receipt read
  /// out of order is useless. Sort by vertical position, then by
  /// horizontal — which also keeps an item and its price on one line.
  String _inReadingOrder(RecognizedText result) {
    final lines = <TextLine>[
      for (final block in result.blocks) ...block.lines,
    ];
    lines.sort((a, b) {
      final dy = a.boundingBox.top.compareTo(b.boundingBox.top);
      // Within a line's height, treat them as the same row.
      if ((a.boundingBox.top - b.boundingBox.top).abs() <
          a.boundingBox.height * 0.6) {
        return a.boundingBox.left.compareTo(b.boundingBox.left);
      }
      return dy;
    });

    final out = StringBuffer();
    double? rowTop;
    for (final line in lines) {
      final top = line.boundingBox.top.toDouble();
      if (rowTop != null &&
          (top - rowTop).abs() < line.boundingBox.height * 0.6) {
        out.write('  ');            // same row: keep it on one line
      } else if (rowTop != null) {
        out.writeln();
      }
      out.write(line.text);
      rowTop = top;
    }
    return out.toString();
  }
}
