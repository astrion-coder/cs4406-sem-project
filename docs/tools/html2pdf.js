// Render an HTML file to an A4 PDF with AppKit. Usage: osascript -l JavaScript html2pdf.js in.html out.pdf
ObjC.import('AppKit');

function run(argv) {
  const [inPath, outPath] = argv;
  const page = { w: 595, h: 842, margin: 54 };
  const html = $.NSData.dataWithContentsOfFile(inPath);
  const text = $.NSAttributedString.alloc.initWithHTMLDocumentAttributes(html, null);

  const info = $.NSPrintInfo.sharedPrintInfo.copy;
  info.paperSize = $.NSMakeSize(page.w, page.h);
  info.topMargin = page.margin;
  info.bottomMargin = page.margin;
  info.leftMargin = page.margin;
  info.rightMargin = page.margin;
  info.horizontalPagination = $.NSFitPagination;
  info.verticallyCentered = false;
  info.jobDisposition = $.NSPrintSaveJob;
  info.dictionary.setObjectForKey($.NSURL.fileURLWithPath(outPath), $.NSPrintJobSavingURL);

  const view = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, page.w - 2 * page.margin, 10));
  view.textStorage.setAttributedString(text);
  view.sizeToFit;

  const op = $.NSPrintOperation.printOperationWithViewPrintInfo(view, info);
  op.showsPrintPanel = false;
  op.showsProgressPanel = false;
  if (!op.runOperation) throw new Error('AppKit print operation failed');
}
