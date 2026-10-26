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
  /// out of order is useless. Group lines into ROWS by vertical centre,
  /// then read each row left to right, so an item stays beside its
  /// price.
  ///
  /// The first version of this compared each line against the PREVIOUS
  /// line's top edge and then moved the reference to it. That chains:
  /// row A pulls in row B because they are close, row B then pulls in
  /// row C, and three separate purchases arrive glued into one string.
  /// On a real Walmart slip it produced
  ///
  ///   0.98 R  GV BFLOR 12 ... 1.16 R  GV BRUSPR 12 ...
  ///
  /// — three rows in one — while the prices either side were left
  /// stranded on lines of their own, and the items after it ended up
  /// carrying their neighbour's price.
  ///
  /// Three things fix it: a row anchor that is FIXED when the row opens
  /// rather than dragged along by each new member; the vertical CENTRE
  /// rather than the top edge, so a tall block and a short one on the
  /// same visual row still match; and a tolerance taken from the median
  /// line height, so one oversized block (a logo, a barcode caption)
  /// cannot widen the band for the whole receipt.
  String _inReadingOrder(RecognizedText result) {
    final lines = <TextLine>[
      for (final block in result.blocks) ...block.lines,
    ];
    if (lines.isEmpty) return '';

    double centreY(TextLine l) =>
        l.boundingBox.top + l.boundingBox.height / 2;

    final heights = [for (final l in lines) l.boundingBox.height.toDouble()]
      ..sort();
    final medianHeight = heights[heights.length ~/ 2];
    // Half a line height: wide enough to absorb the wobble in a photo
    // of curled paper, tight enough that adjacent rows stay apart.
    final tolerance = medianHeight * 0.5;

    // A single, consistent sort key. The previous comparator returned
    // top-vs-top for some pairs and left-vs-left for others, which is
    // not a total order -- with an inconsistent comparator the result
    // is simply undefined.
    final sorted = [...lines]..sort((a, b) => centreY(a).compareTo(centreY(b)));

    final rows = <List<TextLine>>[];
    double? anchor;
    for (final line in sorted) {
      final y = centreY(line);
      if (anchor == null || (y - anchor).abs() > tolerance) {
        rows.add(<TextLine>[line]);
        anchor = y; // fixed for the life of this row
      } else {
        rows.last.add(line);
      }
    }

    for (final row in rows) {
      row.sort((a, b) => a.boundingBox.left.compareTo(b.boundingBox.left));
    }
    return rows
        .map((row) => row.map((l) => l.text).join('  '))
        .join('\n');
  }
}
