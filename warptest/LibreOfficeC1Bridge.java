import com.sun.star.awt.Point;
import com.sun.star.awt.PosSize;
import com.sun.star.awt.Rectangle;
import com.sun.star.awt.Size;
import com.sun.star.awt.XTopWindow;
import com.sun.star.awt.XWindow;
import com.sun.star.beans.PropertyValue;
import com.sun.star.beans.XPropertySet;
import com.sun.star.bridge.XUnoUrlResolver;
import com.sun.star.comp.helper.Bootstrap;
import com.sun.star.container.XEnumeration;
import com.sun.star.container.XIndexAccess;
import com.sun.star.container.XNameAccess;
import com.sun.star.container.XNameContainer;
import com.sun.star.container.XNamed;
import com.sun.star.document.XDocumentPropertiesSupplier;
import com.sun.star.drawing.XDrawPage;
import com.sun.star.drawing.XDrawPages;
import com.sun.star.drawing.XDrawPagesSupplier;
import com.sun.star.drawing.XDrawView;
import com.sun.star.drawing.XShape;
import com.sun.star.drawing.XShapes;
import com.sun.star.frame.XComponentLoader;
import com.sun.star.frame.XController;
import com.sun.star.frame.XDesktop;
import com.sun.star.frame.XFrame;
import com.sun.star.frame.XModel;
import com.sun.star.lang.XComponent;
import com.sun.star.lang.XMultiServiceFactory;
import com.sun.star.lang.XServiceInfo;
import com.sun.star.sheet.XSpreadsheet;
import com.sun.star.sheet.XSpreadsheetView;
import com.sun.star.sheet.XCalculatable;
import com.sun.star.sheet.XCellRangeAddressable;
import com.sun.star.sheet.XCellRangeData;
import com.sun.star.sheet.XNamedRanges;
import com.sun.star.sheet.XSpreadsheetDocument;
import com.sun.star.sheet.XSpreadsheets;
import com.sun.star.table.CellAddress;
import com.sun.star.table.CellRangeAddress;
import com.sun.star.table.XCell;
import com.sun.star.table.XCellRange;
import com.sun.star.table.XTableCharts;
import com.sun.star.table.XTableChartsSupplier;
import com.sun.star.text.ControlCharacter;
import com.sun.star.text.XDocumentIndex;
import com.sun.star.text.XDocumentIndexesSupplier;
import com.sun.star.text.XText;
import com.sun.star.text.XTextContent;
import com.sun.star.text.XTextCursor;
import com.sun.star.text.XTextDocument;
import com.sun.star.text.XTextRange;
import com.sun.star.text.XTextTable;
import com.sun.star.text.XTextTablesSupplier;
import com.sun.star.uno.UnoRuntime;
import com.sun.star.uno.XComponentContext;
import com.sun.star.util.XCloseable;
import com.sun.star.util.XModifiable;
import com.sun.star.frame.XStorable;

import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Dependency-free UNO bridge for the persistent LibreOffice C1 desktop session.
 *
 * The bridge is intentionally narrow: Calc quarterly-budget and Impress status-deck
 * target shapes plus their registered semantic assertions. Unsupported commands are
 * failures, never scripted fallbacks. Each invocation connects to the already-running
 * isolated office process, performs one target-only restore or read-only probe, and
 * exits while LibreOffice stays alive for Agent-S.
 */
public final class LibreOfficeC1Bridge {
    private static final String INPUT_HEADER = "C1_DESKTOP_V1";
    private static final String OUTPUT_HEADER = "C1_DESKTOP_RESULT_V1";
    private static final String SOURCE = "libreoffice_live_uno_probe_v1";

    private static final class Record {
        final String kind;
        final List<String> values;

        Record(String kind, List<String> values) {
            this.kind = kind;
            this.values = values;
        }
    }

    private static final class Check {
        final String name;
        final boolean ok;
        final String detail;

        Check(String name, boolean ok, String detail) {
            this.name = name;
            this.ok = ok;
            this.detail = detail;
        }
    }

    private static final class Result {
        final List<Check> checks = new ArrayList<>();
        final List<String> errors = new ArrayList<>();
        String documentPath = null;
        String documentIdentity = null;
        String documentUrl = null;
        Boolean documentModified = null;
        String sessionIdentity = null;

        boolean passed() {
            if (checks.isEmpty()) {
                return false;
            }
            for (Check check : checks) {
                if (!check.ok) {
                    return false;
                }
            }
            return errors.isEmpty();
        }
    }

    private final List<Record> records;
    private final XDesktop desktop;
    private final String pipeName;

    private LibreOfficeC1Bridge(List<Record> records, XDesktop desktop, String pipeName) {
        this.records = records;
        this.desktop = desktop;
        this.pipeName = pipeName;
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            throw new IllegalArgumentException(
                "LibreOfficeC1Bridge requires <pipe-name> <request> <response>"
            );
        }
        String pipeName = args[0];
        Path requestPath = Paths.get(args[1]).toAbsolutePath().normalize();
        Path responsePath = Paths.get(args[2]).toAbsolutePath().normalize();
        Result result = new Result();
        int exitCode = 0;
        try {
            List<Record> records = readProtocol(requestPath);
            XDesktop desktop = connect(pipeName);
            LibreOfficeC1Bridge bridge = new LibreOfficeC1Bridge(records, desktop, pipeName);
            result = bridge.execute();
        } catch (Throwable error) {
            result.errors.add(error.getClass().getSimpleName() + ": " + safe(error.getMessage()));
            result.checks.add(
                new Check("bridge.exception", false, error.getClass().getSimpleName() + ": " + safe(error.getMessage()))
            );
            exitCode = 1;
        }
        writeResult(responsePath, result);
        // The UNO Java remote bridge owns a non-daemon MessageDispatcher thread.
        // Returning from main therefore leaves this short-lived command helper
        // alive even though the response is complete, causing the Python caller
        // to time out.  Every invocation is deliberately single-command, so end
        // the helper explicitly after the response has been flushed.
        System.exit(exitCode);
    }

    private Result execute() throws Exception {
        Result result = new Result();
        String command = first("COMMAND");
        switch (command) {
            case "ping":
                result.checks.add(new Check("bridge.ready", true, "UNO bridge is connected to the live office"));
                return result;
            case "restore_target":
                return restoreTarget();
            case "probe_target":
                return probeTarget();
            case "probe_assertions":
                return probeAssertions();
            case "probe_document_state":
                return probeDocumentState();
            case "prepare_visual_capture":
                return prepareVisualCapture();
            case "apply_actions":
                return applyActions();
            case "save_document":
                return saveDocument();
            case "reopen_document":
                return reopenDocument();
            case "onboarding_roundtrip":
                return onboardingRoundtrip();
            case "export_pdf":
                return exportPdf();
            case "shutdown":
                boolean terminated = desktop.terminate();
                result.checks.add(
                    new Check("bridge.shutdown", terminated, terminated ? "office termination accepted" : "office termination was vetoed")
                );
                return result;
            default:
                result.checks.add(new Check("command", false, "unsupported command: " + command));
                return result;
        }
    }

    private Result prepareVisualCapture() throws Exception {
        String kind = first("KIND");
        String label = first("VISUAL_LABEL");
        if (!"before".equals(label) && !"after".equals(label)) {
            throw new IllegalArgumentException(
                "visual capture label must be before or after"
            );
        }
        XComponent component = findDocumentComponent(kind);
        if (component == null) {
            throw new IllegalStateException(
                "visual capture has no live document of kind " + kind
            );
        }
        XModel model = UnoRuntime.queryInterface(XModel.class, component);
        XController controller = model == null ? null : model.getCurrentController();
        if (controller == null) {
            throw new IllegalStateException(
                "visual capture document has no current controller"
            );
        }

        Result result = new Result();
        if ("calc_spreadsheet".equals(kind)) {
            String sheetName = optionalFirst("ACTIVE_SHEET");
            XSpreadsheetDocument document = UnoRuntime.queryInterface(
                XSpreadsheetDocument.class, component
            );
            XNameAccess sheets = UnoRuntime.queryInterface(
                XNameAccess.class, document.getSheets()
            );
            if (sheetName == null || sheetName.isBlank()) {
                sheetName = sheets.getElementNames()[0];
            }
            boolean exists = sheets.hasByName(sheetName);
            XSpreadsheetView view = UnoRuntime.queryInterface(
                XSpreadsheetView.class, controller
            );
            if (!exists || view == null) {
                throw new IllegalArgumentException(
                    "visual capture Calc sheet is unavailable: " + sheetName
                );
            }
            XSpreadsheet sheet = UnoRuntime.queryInterface(
                XSpreadsheet.class, sheets.getByName(sheetName)
            );
            view.setActiveSheet(sheet);
            result.checks.add(
                new Check(
                    "visual.active_sheet",
                    true,
                    "activated Calc sheet " + sheetName
                )
            );
        } else if ("impress_presentation".equals(kind)) {
            int slide = Integer.parseInt(first("ACTIVE_SLIDE"));
            XDrawPages pages = drawPages(component);
            if (slide < 1 || slide > pages.getCount()) {
                throw new IllegalArgumentException(
                    "visual capture slide is outside the deck: " + slide
                );
            }
            XDrawView view = UnoRuntime.queryInterface(XDrawView.class, controller);
            XDrawPage page = UnoRuntime.queryInterface(
                XDrawPage.class, pages.getByIndex(slide - 1)
            );
            if (view == null || page == null) {
                throw new IllegalStateException(
                    "visual capture Impress controller has no draw view"
                );
            }
            view.setCurrentPage(page);
            result.checks.add(
                new Check(
                    "visual.active_slide",
                    true,
                    "activated Impress slide " + slide
                )
            );
        } else if ("writer_document".equals(kind)) {
            result.checks.add(
                new Check(
                    "visual.writer_document",
                    true,
                    "Writer document controller is ready"
                )
            );
        } else {
            throw new IllegalArgumentException(
                "unsupported visual capture document kind: " + kind
            );
        }

        bringToFront(component);
        recordDocumentState(result, component);
        result.checks.add(
            new Check(
                "visual.document_activated",
                true,
                "activated " + kind + " for " + label + " capture"
            )
        );
        return result;
    }

    private Result restoreTarget() throws Exception {
        String kind = first("KIND");
        XComponent previous = desktop.getCurrentComponent();
        if (previous != null) {
            closeComponent(previous);
        }
        XComponentLoader loader = UnoRuntime.queryInterface(XComponentLoader.class, desktop);
        if (loader == null) {
            throw new IllegalStateException("live desktop has no XComponentLoader");
        }
        String factory;
        if ("calc_spreadsheet".equals(kind)) {
            factory = "private:factory/scalc";
        } else if ("impress_presentation".equals(kind)) {
            factory = "private:factory/simpress";
        } else if ("writer_document".equals(kind)) {
            factory = "private:factory/swriter";
        } else {
            throw new IllegalArgumentException("unsupported LibreOffice target kind: " + kind);
        }
        PropertyValue hidden = new PropertyValue();
        hidden.Name = "Hidden";
        hidden.Value = Boolean.FALSE;
        XComponent component = loader.loadComponentFromURL(
            factory, "_blank", 0, new PropertyValue[] { hidden }
        );
        if (component == null) {
            throw new IllegalStateException("LibreOffice did not create the requested document");
        }
        if ("calc_spreadsheet".equals(kind)) {
            restoreCalc(component);
        } else if ("writer_document".equals(kind)) {
            restoreWriter(component);
        } else {
            restoreImpress(component);
        }
        // Leave the target as the live, unsaved document.  Headed macOS LibreOffice
        // serializes XStorable calls through the UI loop; issuing storeAsURL or
        // storeToURL from a short-lived remote Java command can block indefinitely.
        // Saving/reopening belongs to the policy transition for these C1 tasks, so
        // doing it here would also leak task actions into target-only restoration.
        bringToFront(component);

        Result result = probeTarget(component);
        recordDocumentState(result, component);
        result.checks.add(0, new Check("restore.target_only", true, "restored target without executing task actions"));
        return result;
    }

    private Result probeTarget() throws Exception {
        String kind = first("KIND");
        XComponent component = findDocumentComponent(kind);
        if (component == null) {
            Result result = new Result();
            recordDocumentState(result, null);
            result.checks.add(
                new Check(
                    "target.component",
                    false,
                    "LibreOffice has no current document component"
                )
            );
            return result;
        }
        bringToFront(component);
        Result result = probeTarget(component);
        recordDocumentState(result, component);
        return result;
    }

    private Result probeTarget(XComponent component) throws Exception {
        String kind = first("KIND");
        Result result = new Result();
        if ("writer_document".equals(kind)) {
            XTextDocument document = UnoRuntime.queryInterface(
                XTextDocument.class, component
            );
            result.checks.add(
                new Check("target.kind", document != null, "expected live Writer document")
            );
            if (document == null) {
                return result;
            }
            String body = document.getText().getString();
            for (Record record : recordsOf("PARAGRAPH")) {
                String expected = record.values.get(0);
                result.checks.add(
                    new Check(
                        "target.paragraph",
                        body.contains(expected),
                        "expected text fragment=" + expected
                    )
                );
            }
            XTextTablesSupplier supplier = UnoRuntime.queryInterface(
                XTextTablesSupplier.class, component
            );
            XNameAccess tables = supplier == null ? null : supplier.getTextTables();
            for (Record record : recordsOf("TABLE")) {
                String tableName = record.values.get(0);
                boolean exists = tables != null && tables.hasByName(tableName);
                result.checks.add(
                    new Check(
                        "target.table[" + tableName + "]",
                        exists,
                        exists ? "table exists" : "table is missing"
                    )
                );
            }
            return result;
        }
        if ("calc_spreadsheet".equals(kind)) {
            XSpreadsheetDocument document = UnoRuntime.queryInterface(XSpreadsheetDocument.class, component);
            result.checks.add(
                new Check("target.kind", document != null, "expected live Calc spreadsheet")
            );
            if (document == null) {
                return result;
            }
            XSpreadsheets sheets = document.getSheets();
            XNameAccess names = UnoRuntime.queryInterface(XNameAccess.class, sheets);
            for (Record record : recordsOf("SHEET")) {
                String sheetName = record.values.get(0);
                result.checks.add(
                    new Check(
                        "target.sheet[" + sheetName + "]",
                        names.hasByName(sheetName),
                        names.hasByName(sheetName) ? "sheet exists" : "sheet is missing"
                    )
                );
            }
            for (Record record : recordsOf("CELL")) {
                checkCalcCell(result, names, record, "target.cell");
            }
            for (Record record : recordsOf("FORMULA")) {
                String sheetName = record.values.get(0);
                String address = record.values.get(1);
                String expected = record.values.get(2);
                if (!names.hasByName(sheetName)) {
                    result.checks.add(new Check("target.formula[" + sheetName + "." + address + "]", false, "sheet is missing"));
                    continue;
                }
                XCell cell = cell(names, sheetName, address);
                String actual = cell.getFormula();
                result.checks.add(
                    new Check(
                        "target.formula[" + sheetName + "." + address + "]",
                        canonicalFormula(actual).equals(canonicalFormula(expected)),
                        "actual=" + actual + " expected=" + expected
                    )
                );
            }
            return result;
        }
        if ("impress_presentation".equals(kind)) {
            XDrawPages pages = drawPages(component);
            int expectedCount = Integer.parseInt(first("SLIDE_COUNT"));
            int actualCount = pages.getCount();
            result.checks.add(
                new Check("target.slide_count", actualCount == expectedCount, "actual=" + actualCount + " expected=" + expectedCount)
            );
            for (Record record : recordsOf("SLIDE_TEXT")) {
                int slide = Integer.parseInt(record.values.get(0));
                String role = record.values.get(1);
                String expected = record.values.get(2);
                if (slide < 1 || slide > actualCount) {
                    result.checks.add(new Check("target.slide[" + slide + "]." + role, false, "slide is missing"));
                    continue;
                }
                String actual = slideText(pages, slide);
                result.checks.add(
                    new Check(
                        "target.slide[" + slide + "]." + role,
                        actual.contains(expected),
                        "expected text fragment=" + expected
                    )
                );
            }
            return result;
        }
        result.checks.add(new Check("target.kind", false, "unsupported target kind: " + kind));
        return result;
    }

    private Result probeAssertions() throws Exception {
        XComponent component = findDocumentComponent(assertionDocumentKind());
        Result result = new Result();
        if (component == null) {
            result.checks.add(
                new Check(
                    "assertions.component",
                    false,
                    "LibreOffice has no current document component"
                )
            );
        } else {
            bringToFront(component);
            for (Record record : recordsOf("ASSERT")) {
                String type = record.values.get(0);
                switch (type) {
                case "libreoffice_text_contains": {
                    XTextDocument document = UnoRuntime.queryInterface(
                        XTextDocument.class, component
                    );
                    String expected = record.values.get(1);
                    boolean ok = document != null
                        && document.getText().getString().contains(expected);
                    result.checks.add(
                        new Check(type, ok, "expected text fragment=" + expected)
                    );
                    break;
                }
                case "libreoffice_toc_contains": {
                    XTextDocument document = UnoRuntime.queryInterface(
                        XTextDocument.class, component
                    );
                    String expected = record.values.get(1);
                    boolean ok = document != null
                        && writerIndexContains(document, expected);
                    result.checks.add(
                        new Check(
                            type,
                            ok,
                            "expected native index text fragment=" + expected
                        )
                    );
                    break;
                }
                case "libreoffice_table_cell_equals": {
                    XTextTablesSupplier supplier = UnoRuntime.queryInterface(
                        XTextTablesSupplier.class, component
                    );
                    String tableName = record.values.get(1);
                    String cellName = record.values.get(2);
                    String expected = record.values.get(3);
                    String actual = writerTableCell(
                        supplier, tableName, cellName
                    ).getString();
                    result.checks.add(
                        new Check(
                            type,
                            actual.equals(expected),
                            "actual=" + actual + " expected=" + expected
                        )
                    );
                    break;
                }
                case "libreoffice_pdf_exists": {
                    Path pdf = containedArtifact(record.values.get(1));
                    boolean exists = Files.isRegularFile(pdf) && Files.size(pdf) > 0;
                    result.checks.add(
                        new Check(
                            type,
                            exists,
                            exists ? pdf.toString() : "PDF is missing: " + pdf
                        )
                    );
                    break;
                }
                case "libreoffice_cell_value_equals": {
                    XSpreadsheetDocument document = UnoRuntime.queryInterface(XSpreadsheetDocument.class, component);
                    if (document == null) {
                        result.checks.add(new Check(type, false, "current component is not Calc"));
                        break;
                    }
                    XNameAccess names = UnoRuntime.queryInterface(XNameAccess.class, document.getSheets());
                    Record cellRecord = new Record(
                        "CELL",
                        List.of(record.values.get(1), record.values.get(2), record.values.get(3), record.values.get(4))
                    );
                    checkCalcCell(result, names, cellRecord, "assertion.cell_value_equals");
                    break;
                }
                case "libreoffice_sheet_exists": {
                    XSpreadsheetDocument document = UnoRuntime.queryInterface(XSpreadsheetDocument.class, component);
                    if (document == null) {
                        result.checks.add(new Check(type, false, "current component is not Calc"));
                        break;
                    }
                    XNameAccess names = UnoRuntime.queryInterface(XNameAccess.class, document.getSheets());
                    String sheet = record.values.get(1);
                    result.checks.add(new Check(type, names.hasByName(sheet), "sheet=" + sheet));
                    break;
                }
                case "libreoffice_formula_equals": {
                    XSpreadsheetDocument document = UnoRuntime.queryInterface(
                        XSpreadsheetDocument.class, component
                    );
                    XNameAccess names = UnoRuntime.queryInterface(
                        XNameAccess.class, document.getSheets()
                    );
                    XCell formulaCell = cell(
                        names, record.values.get(1), record.values.get(2)
                    );
                    String actual = formulaCell.getFormula();
                    String expected = record.values.get(3);
                    result.checks.add(
                        new Check(
                            type,
                            canonicalFormula(actual).equals(canonicalFormula(expected)),
                            "actual=" + actual + " expected=" + expected
                        )
                    );
                    break;
                }
                case "libreoffice_named_range_exists": {
                    XNameAccess names = UnoRuntime.queryInterface(
                        XNameAccess.class, namedRanges(component)
                    );
                    String name = record.values.get(1);
                    boolean exists = names != null && names.hasByName(name);
                    result.checks.add(new Check(type, exists, "name=" + name));
                    break;
                }
                case "libreoffice_chart_exists": {
                    XSpreadsheetDocument document = UnoRuntime.queryInterface(
                        XSpreadsheetDocument.class, component
                    );
                    XNameAccess sheets = UnoRuntime.queryInterface(
                        XNameAccess.class, document.getSheets()
                    );
                    XSpreadsheet sheet = UnoRuntime.queryInterface(
                        XSpreadsheet.class, sheets.getByName(record.values.get(1))
                    );
                    XTableChartsSupplier supplier = UnoRuntime.queryInterface(
                        XTableChartsSupplier.class, sheet
                    );
                    XNameAccess charts = supplier == null
                        ? null
                        : UnoRuntime.queryInterface(
                            XNameAccess.class, supplier.getCharts()
                        );
                    String name = record.values.get(2);
                    boolean exists = charts != null && charts.hasByName(name);
                    result.checks.add(new Check(type, exists, "name=" + name));
                    break;
                }
                case "libreoffice_slide_count_equals": {
                    XDrawPages pages = drawPages(component);
                    int expected = Integer.parseInt(record.values.get(1));
                    int actual = pages.getCount();
                    result.checks.add(new Check(type, actual == expected, "actual=" + actual + " expected=" + expected));
                    break;
                }
                case "libreoffice_slide_title_equals": {
                    XDrawPages pages = drawPages(component);
                    int slide = Integer.parseInt(record.values.get(1));
                    String expected = record.values.get(2);
                    XText title = firstTextShape(slideShapes(pages, slide));
                    String actual = title == null ? "" : title.getString();
                    result.checks.add(
                        new Check(
                            type,
                            actual.equals(expected),
                            "actual=" + actual + " expected=" + expected
                        )
                    );
                    break;
                }
                case "libreoffice_slide_text_contains":
                case "libreoffice_shape_text_contains": {
                    XDrawPages pages = drawPages(component);
                    int slide = Integer.parseInt(record.values.get(1));
                    String expected = record.values.get(2);
                    if (slide < 1 || slide > pages.getCount()) {
                        result.checks.add(new Check(type, false, "slide is missing: " + slide));
                        break;
                    }
                    String actual = slideText(pages, slide);
                    result.checks.add(new Check(type, actual.contains(expected), "slide=" + slide + " expected fragment=" + expected));
                    break;
                }
                case "no_libreoffice_errors": {
                    boolean priorOk = true;
                    for (Check check : result.checks) {
                        priorOk = priorOk && check.ok;
                    }
                    result.checks.add(new Check(type, priorOk, priorOk ? "no prior UNO probe errors" : "prior UNO probe check failed"));
                    break;
                }
                    default:
                        result.checks.add(new Check(type, false, "unsupported assertion"));
                        break;
                }
            }
        }
        if (result.checks.isEmpty()) {
            result.checks.add(new Check("assertions", false, "assertion list is empty"));
        }
        recordDocumentState(result, component);
        addDeclaredTransitionPersistenceChecks(result);
        return result;
    }

    private Result probeDocumentState() throws Exception {
        String kind = first("KIND");
        XComponent component = findDocumentComponent(kind);
        Result result = new Result();
        boolean present = component != null;
        result.checks.add(
            new Check(
                "document.present",
                present,
                present ? "registered document kind is live" : "registered document kind is not live"
            )
        );
        recordDocumentState(result, component);
        return result;
    }

    private Result applyActions() throws Exception {
        String kind = first("KIND");
        XComponent component = findDocumentComponent(kind);
        if (component == null) {
            throw new IllegalStateException(
                "LibreOffice state transition has no live registered document"
            );
        }
        Result result = new Result();
        List<Record> actions = recordsOf("ACTION");
        if (actions.isEmpty()) {
            result.checks.add(
                new Check("transition.actions_applied", false, "state action list is empty")
            );
            recordDocumentState(result, component);
            return result;
        }
        for (Record action : actions) {
            String type = action.values.get(0);
            if ("libreoffice_open_document".equals(type)) {
                // restore_target already owns the live document.  This registered
                // action proves the workflow opened the declared bounded target.
            } else if ("libreoffice_insert_toc".equals(type)) {
                Set<String> styles = new LinkedHashSet<>();
                for (int index = 2; index < action.values.size(); index++) {
                    styles.add(action.values.get(index));
                }
                insertWriterIndex(
                    component,
                    action.values.get(1),
                    styles
                );
            } else if ("libreoffice_set_table_cell".equals(type)) {
                XTextTablesSupplier supplier = UnoRuntime.queryInterface(
                    XTextTablesSupplier.class, component
                );
                writerTableCell(
                    supplier, action.values.get(1), action.values.get(2)
                ).setString(action.values.get(3));
            } else if ("libreoffice_append_paragraph".equals(type)) {
                appendWriterText(
                    requireWriter(component),
                    "\n" + action.values.get(1)
                );
            } else if ("libreoffice_update_indexes".equals(type)) {
                updateWriterIndexes(component);
            } else if ("libreoffice_set_cell".equals(type)) {
                XSpreadsheetDocument document = UnoRuntime.queryInterface(
                    XSpreadsheetDocument.class, component
                );
                if (document == null) {
                    throw new IllegalStateException("cell action requires Calc");
                }
                XNameAccess names = UnoRuntime.queryInterface(
                    XNameAccess.class, document.getSheets()
                );
                XCell cell = cell(names, action.values.get(1), action.values.get(2));
                setScalar(cell, action.values.get(3), action.values.get(4));
            } else if ("libreoffice_recalculate".equals(type)) {
                XCalculatable calculatable = UnoRuntime.queryInterface(
                    XCalculatable.class, component
                );
                if (calculatable == null) {
                    throw new IllegalStateException("recalculate action requires Calc");
                }
                calculatable.calculateAll();
            } else if ("libreoffice_define_named_range".equals(type)) {
                defineNamedRange(
                    component,
                    action.values.get(1),
                    action.values.get(2),
                    action.values.get(3)
                );
            } else if ("libreoffice_sort_range".equals(type)) {
                sortTwoColumnRange(
                    component,
                    action.values.get(1),
                    action.values.get(2),
                    action.values.get(3),
                    "descending".equalsIgnoreCase(action.values.get(4))
                );
            } else if ("libreoffice_create_chart".equals(type)) {
                createChart(
                    component,
                    action.values.get(1),
                    action.values.get(2),
                    action.values.get(3)
                );
            } else if ("libreoffice_move_slide".equals(type)) {
                moveSlide(
                    component,
                    Integer.parseInt(action.values.get(1)),
                    Integer.parseInt(action.values.get(2))
                );
            } else if ("libreoffice_set_slide_title".equals(type)) {
                XDrawPages pages = drawPages(component);
                XShapes shapes = slideShapes(pages, Integer.parseInt(action.values.get(1)));
                XText title = firstTextShape(shapes);
                if (title == null) {
                    throw new IllegalStateException("slide title shape is missing");
                }
                title.setString(action.values.get(2));
            } else if ("libreoffice_set_slide_bullets".equals(type)) {
                XDrawPages pages = drawPages(component);
                XShapes shapes = slideShapes(pages, Integer.parseInt(action.values.get(1)));
                while (shapes.getCount() > 1) {
                    XShape shape = UnoRuntime.queryInterface(
                        XShape.class, shapes.getByIndex(shapes.getCount() - 1)
                    );
                    shapes.remove(shape);
                }
                for (int index = 2; index < action.values.size(); index++) {
                    addImpressTextShape(
                        component,
                        shapes,
                        "• " + action.values.get(index),
                        1600,
                        3200 + (index - 2) * 1200,
                        23500,
                        1100
                    );
                }
            } else if ("libreoffice_add_shape".equals(type)) {
                if (
                    !"callout".equals(action.values.get(2))
                    && !"rectangle".equals(action.values.get(2))
                ) {
                    throw new IllegalArgumentException(
                        "only the registered Impress callout shape is supported"
                    );
                }
                XDrawPages pages = drawPages(component);
                XShapes shapes = slideShapes(pages, Integer.parseInt(action.values.get(1)));
                addImpressTextShape(
                    component,
                    shapes,
                    action.values.get(3),
                    1600,
                    7200,
                    23500,
                    1500
                );
            } else {
                throw new IllegalArgumentException(
                    "unsupported LibreOffice state action: " + type
                );
            }
            result.checks.add(new Check("action." + type, true, "applied to live document"));
        }
        XCalculatable calculatable = UnoRuntime.queryInterface(
            XCalculatable.class, component
        );
        if (calculatable != null) {
            calculatable.calculateAll();
        }
        result.checks.add(
            new Check(
                "transition.actions_applied",
                true,
                "registered state-managed mutations were applied without save/reopen"
            )
        );
        recordDocumentState(result, component);
        return result;
    }

    private Result reopenDocument() throws Exception {
        String kind = first("KIND");
        XComponent component = findDocumentComponent(kind);
        if (component == null) {
            throw new IllegalStateException(
                "LibreOffice state transition has no registered document to reopen"
            );
        }
        String originIdentity = UnoRuntime.generateOid(component);
        Path artifactDir = Paths.get(first("ARTIFACT_DIR")).toAbsolutePath().normalize();
        Path relative = Paths.get(first("DOCUMENT_PATH"));
        if (relative.isAbsolute()) {
            throw new IllegalArgumentException("reopen document path must be relative");
        }
        Path documentPath = artifactDir.resolve(relative).normalize();
        if (
            !documentPath.startsWith(artifactDir)
            || Files.isSymbolicLink(documentPath)
            || !Files.isRegularFile(documentPath)
        ) {
            throw new IllegalArgumentException(
                "reopen document is not a contained persisted artifact: " + documentPath
            );
        }
        XModel model = UnoRuntime.queryInterface(XModel.class, component);
        XController controller = model == null ? null : model.getCurrentController();
        XFrame frame = controller == null ? null : controller.getFrame();
        XComponentLoader loader = UnoRuntime.queryInterface(XComponentLoader.class, frame);
        if (loader == null) {
            throw new IllegalStateException("live document frame has no XComponentLoader");
        }
        PropertyValue hidden = new PropertyValue();
        hidden.Name = "Hidden";
        hidden.Value = Boolean.FALSE;
        XComponent reopened = loader.loadComponentFromURL(
            documentPath.toUri().toString(),
            "_self",
            0,
            new PropertyValue[] { hidden }
        );
        if (reopened == null) {
            throw new IllegalStateException("LibreOffice did not reopen the persisted document");
        }
        bringToFront(reopened);
        String reopenedIdentity = UnoRuntime.generateOid(reopened);
        Result result = new Result();
        boolean changed = !originIdentity.equals(reopenedIdentity);
        result.checks.add(
            new Check(
                "transition.document_reopened_by_runtime",
                changed,
                changed
                    ? "reopened persisted artifact into a new live component"
                    : "reopen retained the original live component identity"
            )
        );
        recordDocumentState(result, reopened);
        result.documentPath = documentPath.toString();
        return result;
    }

    private Result saveDocument() throws Exception {
        String kind = first("KIND");
        XComponent component = findDocumentComponent(kind);
        if (component == null) {
            throw new IllegalStateException(
                "LibreOffice state transition has no registered document to save"
            );
        }
        Path artifactDir = Paths.get(first("ARTIFACT_DIR")).toAbsolutePath().normalize();
        Path relative = Paths.get(first("DOCUMENT_PATH"));
        if (relative.isAbsolute()) {
            throw new IllegalArgumentException("save document path must be relative");
        }
        Path documentPath = artifactDir.resolve(relative).normalize();
        if (!documentPath.startsWith(artifactDir) || Files.isSymbolicLink(documentPath)) {
            throw new IllegalArgumentException("save document path escaped artifact root");
        }
        Files.createDirectories(documentPath.getParent());
        XStorable storable = UnoRuntime.queryInterface(XStorable.class, component);
        if (storable == null) {
            throw new IllegalStateException("live document has no XStorable interface");
        }
        PropertyValue overwrite = new PropertyValue();
        overwrite.Name = "Overwrite";
        overwrite.Value = Boolean.TRUE;
        storable.storeAsURL(
            documentPath.toUri().toString(), new PropertyValue[] { overwrite }
        );
        Result result = new Result();
        boolean persisted = Files.isRegularFile(documentPath) && Files.size(documentPath) > 0;
        result.checks.add(
            new Check(
                "transition.document_saved_by_runtime",
                persisted,
                persisted ? documentPath.toString() : "document artifact was not persisted"
            )
        );
        recordDocumentState(result, component);
        result.documentPath = documentPath.toString();
        return result;
    }

    private Result onboardingRoundtrip() throws Exception {
        Result combined = new Result();
        Result restored = restoreTarget();
        combined.checks.addAll(restored.checks);
        combined.errors.addAll(restored.errors);
        if (!restored.passed()) {
            return combined;
        }
        Result saved = saveDocument();
        combined.checks.addAll(saved.checks);
        combined.errors.addAll(saved.errors);
        if (!saved.passed()) {
            return combined;
        }
        Result reopened = reopenDocument();
        combined.checks.addAll(reopened.checks);
        combined.errors.addAll(reopened.errors);
        if (!reopened.passed()) {
            return combined;
        }
        Result probed = probeTarget();
        combined.checks.addAll(probed.checks);
        combined.errors.addAll(probed.errors);
        combined.documentPath = reopened.documentPath;
        combined.documentIdentity = probed.documentIdentity;
        combined.documentUrl = probed.documentUrl;
        combined.documentModified = probed.documentModified;
        combined.sessionIdentity = probed.sessionIdentity;
        return combined;
    }

    private Result exportPdf() throws Exception {
        String kind = first("KIND");
        XComponent component = findDocumentComponent(kind);
        if (component == null) {
            throw new IllegalStateException(
                "LibreOffice PDF export has no registered live document"
            );
        }
        List<Record> actions = recordsOf("ACTION");
        if (
            actions.size() != 1
            || actions.get(0).values.size() < 2
            || !"libreoffice_export_pdf".equals(actions.get(0).values.get(0))
        ) {
            throw new IllegalArgumentException(
                "PDF export requires exactly one registered export action"
            );
        }
        Path pdfPath = containedArtifact(actions.get(0).values.get(1));
        Files.createDirectories(pdfPath.getParent());
        XStorable storable = UnoRuntime.queryInterface(XStorable.class, component);
        if (storable == null) {
            throw new IllegalStateException("live document has no XStorable interface");
        }
        String filter = "writer_document".equals(kind)
            ? "writer_pdf_Export"
            : ("calc_spreadsheet".equals(kind)
                ? "calc_pdf_Export"
                : "impress_pdf_Export");
        PropertyValue filterName = new PropertyValue();
        filterName.Name = "FilterName";
        filterName.Value = filter;
        PropertyValue overwrite = new PropertyValue();
        overwrite.Name = "Overwrite";
        overwrite.Value = Boolean.TRUE;
        storable.storeToURL(
            pdfPath.toUri().toString(),
            new PropertyValue[] { filterName, overwrite }
        );
        boolean exported = Files.isRegularFile(pdfPath) && Files.size(pdfPath) > 0;
        Result result = new Result();
        result.checks.add(
            new Check(
                "action.libreoffice_export_pdf",
                exported,
                exported ? pdfPath.toString() : "PDF export is missing"
            )
        );
        recordDocumentState(result, component);
        return result;
    }

    private void recordDocumentState(Result result, XComponent component) {
        result.sessionIdentity = pipeName;
        if (component == null) {
            return;
        }
        result.documentIdentity = UnoRuntime.generateOid(component);
        XModel model = UnoRuntime.queryInterface(XModel.class, component);
        result.documentUrl = model == null ? null : safe(model.getURL());
        XModifiable modifiable = UnoRuntime.queryInterface(XModifiable.class, component);
        result.documentModified = modifiable == null ? null : modifiable.isModified();
    }

    private void addDeclaredTransitionPersistenceChecks(Result result) {
        String originDocumentIdentity = optionalFirst("ORIGIN_DOCUMENT_IDENTITY");
        String originSessionIdentity = optionalFirst("ORIGIN_SESSION_IDENTITY");
        boolean sameSession = originSessionIdentity != null
            && !originSessionIdentity.isBlank()
            && originSessionIdentity.equals(pipeName)
            && originSessionIdentity.equals(result.sessionIdentity);

        String currentUrl = safe(result.documentUrl);
        boolean savedUrl = currentUrl.startsWith("file:");

        Path persistedPath = null;
        String persistedDetail = "current document URL is not a valid persisted file";
        if (savedUrl) {
            try {
                persistedPath = Paths.get(URI.create(currentUrl)).toAbsolutePath().normalize();
                persistedDetail = persistedPath.toString();
            } catch (RuntimeException ignored) {
                persistedPath = null;
            }
        }
        boolean persisted = false;
        if (persistedPath != null) {
            try {
                persisted = Files.isRegularFile(persistedPath) && Files.size(persistedPath) > 0;
            } catch (IOException ignored) {
                persisted = false;
            }
        }
        result.documentPath = persisted ? persistedPath.toString() : null;

        boolean notModified = Boolean.FALSE.equals(result.documentModified);

        boolean reopened = originDocumentIdentity != null
            && !originDocumentIdentity.isBlank()
            && result.documentIdentity != null
            && !result.documentIdentity.isBlank()
            && !originDocumentIdentity.equals(result.documentIdentity);
        for (Record record : recordsOf("TRANSITION_CHECK")) {
            String name = record.values.isEmpty() ? "" : record.values.get(0);
            switch (name) {
                case "transition.session_same":
                    result.checks.add(
                        new Check(
                            name,
                            sameSession,
                            sameSession ? "goal probe is bound to the original office session" : "office session identity changed or is missing"
                        )
                    );
                    break;
                case "transition.saved_url":
                    result.checks.add(
                        new Check(
                            name,
                            savedUrl,
                            savedUrl ? currentUrl : "current document has no persisted file URL"
                        )
                    );
                    break;
                case "transition.persisted_file":
                    result.checks.add(
                        new Check(name, persisted, persistedDetail)
                    );
                    break;
                case "transition.not_modified":
                    result.checks.add(
                        new Check(
                            name,
                            notModified,
                            notModified ? "reopened document has no unsaved modifications" : "current document is modified or modification state is unavailable"
                        )
                    );
                    break;
                case "transition.document_reopened":
                    result.checks.add(
                        new Check(
                            name,
                            reopened,
                            reopened ? "current UNO document identity differs from the start document" : "document identity did not change after the transition"
                        )
                    );
                    break;
                default:
                    result.checks.add(
                        new Check(name, false, "unsupported transition persistence check")
                    );
                    break;
            }
        }
    }

    private String assertionDocumentKind() {
        String kind = null;
        for (Record record : recordsOf("ASSERT")) {
            String assertion = record.values.get(0);
            String candidate = null;
            if (
                "libreoffice_text_contains".equals(assertion)
                || "libreoffice_toc_contains".equals(assertion)
                || "libreoffice_table_cell_equals".equals(assertion)
            ) {
                candidate = "writer_document";
            } else if (
                "libreoffice_cell_value_equals".equals(assertion)
                || "libreoffice_sheet_exists".equals(assertion)
                || "libreoffice_formula_equals".equals(assertion)
                || "libreoffice_named_range_exists".equals(assertion)
                || "libreoffice_chart_exists".equals(assertion)
            ) {
                candidate = "calc_spreadsheet";
            } else if (
                "libreoffice_slide_count_equals".equals(assertion)
                || "libreoffice_slide_title_equals".equals(assertion)
                || "libreoffice_slide_text_contains".equals(assertion)
                || "libreoffice_shape_text_contains".equals(assertion)
            ) {
                candidate = "impress_presentation";
            }
            if (candidate != null && kind != null && !candidate.equals(kind)) {
                throw new IllegalArgumentException(
                    "LibreOffice assertions mix Calc and Impress document kinds"
                );
            }
            if (candidate != null) {
                kind = candidate;
            }
        }
        return kind;
    }

    private XComponent findDocumentComponent(String kind) throws Exception {
        XComponent current = desktop.getCurrentComponent();
        if (matchesDocumentKind(current, kind)) {
            return current;
        }
        XEnumeration components = desktop.getComponents().createEnumeration();
        while (components.hasMoreElements()) {
            XComponent candidate = UnoRuntime.queryInterface(
                XComponent.class, components.nextElement()
            );
            if (matchesDocumentKind(candidate, kind)) {
                return candidate;
            }
        }
        return null;
    }

    private static boolean matchesDocumentKind(XComponent component, String kind) {
        if (component == null) {
            return false;
        }
        if (kind == null) {
            return UnoRuntime.queryInterface(XModel.class, component) != null;
        }
        if ("calc_spreadsheet".equals(kind)) {
            return UnoRuntime.queryInterface(XSpreadsheetDocument.class, component) != null;
        }
        if ("impress_presentation".equals(kind)) {
            XServiceInfo services = UnoRuntime.queryInterface(XServiceInfo.class, component);
            return services != null
                && services.supportsService("com.sun.star.presentation.PresentationDocument");
        }
        if ("writer_document".equals(kind)) {
            return UnoRuntime.queryInterface(XTextDocument.class, component) != null;
        }
        return false;
    }

    private void restoreWriter(XComponent component) throws Exception {
        XTextDocument document = requireWriter(component);
        XText text = document.getText();
        XTextCursor cursor = text.createTextCursor();
        boolean firstParagraph = true;
        for (Record record : recordsOf("PARAGRAPH")) {
            if (!firstParagraph) {
                text.insertControlCharacter(
                    cursor,
                    ControlCharacter.PARAGRAPH_BREAK,
                    false
                );
            }
            cursor.gotoEnd(false);
            XPropertySet paragraphProperties = UnoRuntime.queryInterface(
                XPropertySet.class,
                cursor
            );
            if (paragraphProperties == null) {
                throw new IllegalStateException(
                    "Writer paragraph cursor has no XPropertySet interface"
                );
            }
            String style = record.values.size() > 1
                ? record.values.get(1)
                : "";
            paragraphProperties.setPropertyValue(
                "ParaStyleName",
                style.isBlank() ? "Standard" : style
            );
            text.insertString(cursor, record.values.get(0), false);
            firstParagraph = false;
        }
        XMultiServiceFactory factory = UnoRuntime.queryInterface(
            XMultiServiceFactory.class, component
        );
        for (Record tableRecord : recordsOf("TABLE")) {
            String name = tableRecord.values.get(0);
            int rows = Integer.parseInt(tableRecord.values.get(1));
            int columns = Integer.parseInt(tableRecord.values.get(2));
            text.insertString(cursor, "\n", false);
            Object tableObject = factory.createInstance("com.sun.star.text.TextTable");
            XTextTable table = UnoRuntime.queryInterface(
                XTextTable.class, tableObject
            );
            XNamed named = UnoRuntime.queryInterface(XNamed.class, tableObject);
            if (table == null || named == null) {
                throw new IllegalStateException("Writer TextTable lacks required interfaces");
            }
            table.initialize(rows, columns);
            named.setName(name);
            text.insertTextContent(cursor, table, false);
            for (Record cellRecord : recordsOf("TABLE_CELL")) {
                if (!name.equals(cellRecord.values.get(0))) {
                    continue;
                }
                int column = Integer.parseInt(cellRecord.values.get(1));
                int row = Integer.parseInt(cellRecord.values.get(2));
                String cellName = writerCellName(column, row);
                XText cellText = UnoRuntime.queryInterface(
                    XText.class, table.getCellByName(cellName)
                );
                cellText.setString(cellRecord.values.get(3));
            }
            cursor = text.createTextCursor();
            cursor.gotoEnd(false);
        }
    }

    private static XTextDocument requireWriter(XComponent component) {
        XTextDocument document = UnoRuntime.queryInterface(
            XTextDocument.class, component
        );
        if (document == null) {
            throw new IllegalStateException("current component is not Writer");
        }
        return document;
    }

    private static void appendWriterText(XTextDocument document, String value) {
        XText text = document.getText();
        XTextCursor cursor = text.createTextCursor();
        cursor.gotoEnd(false);
        text.insertString(cursor, value, false);
    }

    private static void insertWriterIndex(
        XComponent component,
        String title,
        Set<String> styles
    ) throws Exception {
        XTextDocument document = requireWriter(component);
        XMultiServiceFactory factory = UnoRuntime.queryInterface(
            XMultiServiceFactory.class,
            component
        );
        if (factory == null) {
            throw new IllegalStateException(
                "Writer document has no XMultiServiceFactory interface"
            );
        }
        short level = 0;
        for (String style : styles) {
            if (!style.matches("Heading [1-9]|Heading 10")) {
                throw new IllegalArgumentException(
                    "Writer native index only supports registered heading styles: "
                    + style
                );
            }
            level = (short) Math.max(
                level,
                Integer.parseInt(style.substring("Heading ".length()))
            );
        }
        if (level == 0) {
            level = 10;
        }
        Object indexObject = factory.createInstance(
            "com.sun.star.text.ContentIndex"
        );
        XPropertySet properties = UnoRuntime.queryInterface(
            XPropertySet.class,
            indexObject
        );
        XTextContent content = UnoRuntime.queryInterface(
            XTextContent.class,
            indexObject
        );
        XDocumentIndex index = UnoRuntime.queryInterface(
            XDocumentIndex.class,
            indexObject
        );
        if (properties == null || content == null || index == null) {
            throw new IllegalStateException(
                "Writer ContentIndex lacks required UNO interfaces"
            );
        }
        properties.setPropertyValue("Title", title);
        properties.setPropertyValue("CreateFromOutline", Boolean.TRUE);
        properties.setPropertyValue("Level", Short.valueOf(level));

        XText text = document.getText();
        XTextCursor cursor = text.createTextCursor();
        cursor.gotoEnd(false);
        text.insertControlCharacter(
            cursor,
            ControlCharacter.PARAGRAPH_BREAK,
            false
        );
        cursor.gotoEnd(false);
        text.insertTextContent(cursor, content, false);
        index.update();
    }

    private static void updateWriterIndexes(XComponent component)
        throws Exception {
        XDocumentIndexesSupplier supplier = UnoRuntime.queryInterface(
            XDocumentIndexesSupplier.class,
            component
        );
        if (supplier == null) {
            throw new IllegalStateException(
                "current component has no Writer document indexes"
            );
        }
        XIndexAccess indexes = supplier.getDocumentIndexes();
        if (indexes == null || indexes.getCount() == 0) {
            throw new IllegalStateException(
                "Writer document has no native index to update"
            );
        }
        for (int offset = 0; offset < indexes.getCount(); offset++) {
            XDocumentIndex index = UnoRuntime.queryInterface(
                XDocumentIndex.class,
                indexes.getByIndex(offset)
            );
            if (index == null) {
                throw new IllegalStateException(
                    "Writer document index lacks XDocumentIndex"
                );
            }
            index.update();
        }
    }

    private static boolean writerIndexContains(
        XTextDocument document,
        String expected
    ) throws Exception {
        XDocumentIndexesSupplier supplier = UnoRuntime.queryInterface(
            XDocumentIndexesSupplier.class,
            document
        );
        if (supplier == null) {
            return false;
        }
        XIndexAccess indexes = supplier.getDocumentIndexes();
        if (indexes == null) {
            return false;
        }
        for (int offset = 0; offset < indexes.getCount(); offset++) {
            XDocumentIndex index = UnoRuntime.queryInterface(
                XDocumentIndex.class,
                indexes.getByIndex(offset)
            );
            if (index == null) {
                continue;
            }
            XTextRange anchor = index.getAnchor();
            if (anchor == null) {
                continue;
            }
            XText indexText = anchor.getText();
            XTextCursor cursor = indexText.createTextCursorByRange(
                anchor.getStart()
            );
            cursor.gotoRange(anchor.getEnd(), true);
            if (cursor.getString().contains(expected)) {
                return true;
            }
        }
        return false;
    }

    private static XText writerTableCell(
        XTextTablesSupplier supplier,
        String tableName,
        String cellName
    ) throws Exception {
        if (supplier == null) {
            throw new IllegalStateException("current component has no Writer tables");
        }
        XNameAccess tables = supplier.getTextTables();
        if (!tables.hasByName(tableName)) {
            throw new IllegalArgumentException("Writer table is missing: " + tableName);
        }
        XTextTable table = UnoRuntime.queryInterface(
            XTextTable.class, tables.getByName(tableName)
        );
        XText text = UnoRuntime.queryInterface(
            XText.class, table.getCellByName(cellName)
        );
        if (text == null) {
            throw new IllegalStateException(
                "Writer table cell has no XText interface: "
                + tableName + "." + cellName
            );
        }
        return text;
    }

    private static String writerCellName(int column, int row) {
        if (column < 1 || column > 26 || row < 1) {
            throw new IllegalArgumentException("unsupported Writer table cell index");
        }
        return Character.toString((char) ('A' + column - 1)) + row;
    }

    private Path containedArtifact(String rawPath) throws Exception {
        Path artifactDir = Paths.get(first("ARTIFACT_DIR"))
            .toAbsolutePath().normalize();
        Path relative = Paths.get(rawPath);
        if (relative.isAbsolute() || relative.toString().isBlank()) {
            throw new IllegalArgumentException(
                "LibreOffice artifact path must be a non-empty relative path"
            );
        }
        Path resolved = artifactDir.resolve(relative).normalize();
        if (!resolved.startsWith(artifactDir) || Files.isSymbolicLink(resolved)) {
            throw new IllegalArgumentException(
                "LibreOffice artifact path escaped its registered root"
            );
        }
        return resolved;
    }

    private static XNamedRanges namedRanges(XComponent component)
        throws Exception {
        XPropertySet properties = UnoRuntime.queryInterface(
            XPropertySet.class, component
        );
        if (properties == null) {
            throw new IllegalStateException(
                "Calc document has no XPropertySet interface"
            );
        }
        XNamedRanges ranges = UnoRuntime.queryInterface(
            XNamedRanges.class, properties.getPropertyValue("NamedRanges")
        );
        if (ranges == null) {
            throw new IllegalStateException("Calc document has no named ranges");
        }
        return ranges;
    }

    private static void defineNamedRange(
        XComponent component,
        String name,
        String sheetName,
        String rangeName
    ) throws Exception {
        XNamedRanges ranges = namedRanges(component);
        if (ranges.hasByName(name)) {
            ranges.removeByName(name);
        }
        String absoluteRange = rangeName.replaceAll(
            "([A-Za-z]+)([0-9]+)",
            "\\$$1\\$$2"
        );
        String absolute = "$" + sheetName + "." + absoluteRange;
        ranges.addNewByName(
            name,
            absolute,
            new CellAddress((short) 0, 0, 0),
            0
        );
    }

    private static void sortTwoColumnRange(
        XComponent component,
        String sheetName,
        String rangeName,
        String key,
        boolean descending
    ) throws Exception {
        XSpreadsheetDocument document = UnoRuntime.queryInterface(
            XSpreadsheetDocument.class, component
        );
        XNameAccess sheets = UnoRuntime.queryInterface(
            XNameAccess.class, document.getSheets()
        );
        XSpreadsheet sheet = UnoRuntime.queryInterface(
            XSpreadsheet.class, sheets.getByName(sheetName)
        );
        XCellRange range = sheet.getCellRangeByName(rangeName);
        XCellRangeData data = UnoRuntime.queryInterface(
            XCellRangeData.class, range
        );
        Object[][] rows = data.getDataArray();
        int keyIndex = Math.max(0, Character.toUpperCase(key.charAt(0))
            - Character.toUpperCase(rangeName.charAt(0)));
        java.util.Arrays.sort(rows, (left, right) -> {
            double leftValue = Double.parseDouble(left[keyIndex].toString());
            double rightValue = Double.parseDouble(right[keyIndex].toString());
            int compared = Double.compare(leftValue, rightValue);
            return descending ? -compared : compared;
        });
        data.setDataArray(rows);
    }

    private static void createChart(
        XComponent component,
        String sheetName,
        String chartName,
        String rangeName
    ) throws Exception {
        XSpreadsheetDocument document = UnoRuntime.queryInterface(
            XSpreadsheetDocument.class, component
        );
        XNameAccess sheets = UnoRuntime.queryInterface(
            XNameAccess.class, document.getSheets()
        );
        XSpreadsheet sheet = UnoRuntime.queryInterface(
            XSpreadsheet.class, sheets.getByName(sheetName)
        );
        XTableChartsSupplier supplier = UnoRuntime.queryInterface(
            XTableChartsSupplier.class, sheet
        );
        XTableCharts charts = supplier.getCharts();
        if (charts.hasByName(chartName)) {
            charts.removeByName(chartName);
        }
        XCellRangeAddressable addressable = UnoRuntime.queryInterface(
            XCellRangeAddressable.class,
            sheet.getCellRangeByName(rangeName)
        );
        CellRangeAddress address = addressable.getRangeAddress();
        charts.addNewByName(
            chartName,
            new Rectangle(11000, 1000, 14000, 9000),
            new CellRangeAddress[] { address },
            true,
            true
        );
    }

    private static void moveSlide(
        XComponent component,
        int from,
        int to
    ) throws Exception {
        XDrawPages pages = drawPages(component);
        if (
            from < 1 || from > pages.getCount()
            || to < 1 || to > pages.getCount()
        ) {
            throw new IllegalArgumentException("slide move is outside the deck");
        }
        XDrawPage source = UnoRuntime.queryInterface(
            XDrawPage.class, pages.getByIndex(from - 1)
        );
        List<String> text = new ArrayList<>();
        collectText(
            UnoRuntime.queryInterface(XShapes.class, source),
            text
        );
        XDrawPage destination = pages.insertNewByIndex(to - 1);
        XShapes shapes = UnoRuntime.queryInterface(XShapes.class, destination);
        for (int index = 0; index < text.size(); index++) {
            addImpressTextShape(
                component,
                shapes,
                text.get(index),
                index == 0 ? 1200 : 1600,
                index == 0 ? 700 : 3200 + (index - 1) * 1200,
                index == 0 ? 25000 : 23500,
                index == 0 ? 1800 : 1100
            );
        }
        pages.remove(source);
    }

    private void restoreCalc(XComponent component) throws Exception {
        XSpreadsheetDocument document = UnoRuntime.queryInterface(XSpreadsheetDocument.class, component);
        if (document == null) {
            throw new IllegalStateException("factory document is not Calc");
        }
        XSpreadsheets sheets = document.getSheets();
        XNameAccess names = UnoRuntime.queryInterface(XNameAccess.class, sheets);
        XNameContainer container = UnoRuntime.queryInterface(XNameContainer.class, sheets);
        List<Record> sheetRecords = recordsOf("SHEET");
        if (sheetRecords.isEmpty()) {
            throw new IllegalArgumentException("Calc target has no sheets");
        }
        String firstSheetName = sheetRecords.get(0).values.get(0);
        XIndexAccess indexed = UnoRuntime.queryInterface(XIndexAccess.class, sheets);
        if (!names.hasByName(firstSheetName) && indexed.getCount() == 1) {
            XNamed named = UnoRuntime.queryInterface(XNamed.class, indexed.getByIndex(0));
            named.setName(firstSheetName);
        }
        short index = 0;
        Set<String> expectedNames = new LinkedHashSet<>();
        for (Record record : sheetRecords) {
            String name = record.values.get(0);
            expectedNames.add(name);
            if (!names.hasByName(name)) {
                sheets.insertNewByName(name, index);
            }
            index++;
        }
        for (String existing : names.getElementNames()) {
            if (!expectedNames.contains(existing) && container != null) {
                container.removeByName(existing);
            }
        }
        names = UnoRuntime.queryInterface(XNameAccess.class, sheets);
        for (Record record : recordsOf("CELL")) {
            XCell cell = cell(names, record.values.get(0), record.values.get(1));
            setScalar(cell, record.values.get(2), record.values.get(3));
        }
        for (Record record : recordsOf("FORMULA")) {
            XCell cell = cell(names, record.values.get(0), record.values.get(1));
            cell.setFormula(record.values.get(2));
        }
        XCalculatable calculatable = UnoRuntime.queryInterface(XCalculatable.class, document);
        if (calculatable != null) {
            calculatable.calculateAll();
        }
    }

    private void restoreImpress(XComponent component) throws Exception {
        XDrawPages pages = drawPages(component);
        int count = Integer.parseInt(first("SLIDE_COUNT"));
        if (count < 1) {
            throw new IllegalArgumentException("Impress target must contain at least one slide");
        }
        while (pages.getCount() < count) {
            pages.insertNewByIndex(pages.getCount());
        }
        while (pages.getCount() > count) {
            XDrawPage page = UnoRuntime.queryInterface(XDrawPage.class, pages.getByIndex(pages.getCount() - 1));
            pages.remove(page);
        }
        for (int pageIndex = 0; pageIndex < pages.getCount(); pageIndex++) {
            XShapes shapes = UnoRuntime.queryInterface(XShapes.class, pages.getByIndex(pageIndex));
            for (int shapeIndex = shapes.getCount() - 1; shapeIndex >= 0; shapeIndex--) {
                XShape shape = UnoRuntime.queryInterface(XShape.class, shapes.getByIndex(shapeIndex));
                shapes.remove(shape);
            }
        }
        XMultiServiceFactory factory = UnoRuntime.queryInterface(XMultiServiceFactory.class, component);
        if (factory == null) {
            throw new IllegalStateException("Impress document has no service factory");
        }
        for (Record record : recordsOf("SLIDE_TEXT")) {
            int slide = Integer.parseInt(record.values.get(0));
            String role = record.values.get(1);
            String text = record.values.get(2);
            XShapes shapes = UnoRuntime.queryInterface(XShapes.class, pages.getByIndex(slide - 1));
            Object shapeObject = factory.createInstance("com.sun.star.drawing.TextShape");
            XShape shape = UnoRuntime.queryInterface(XShape.class, shapeObject);
            XText shapeText = UnoRuntime.queryInterface(XText.class, shapeObject);
            int current = shapes.getCount();
            if ("title".equals(role)) {
                shape.setPosition(new Point(1200, 700));
                shape.setSize(new Size(25000, 1800));
            } else {
                shape.setPosition(new Point(1600, 3200 + current * 1200));
                shape.setSize(new Size(23500, 1100));
            }
            shapes.add(shape);
            // Impress does not reliably retain XText content assigned while a
            // newly-created TextShape is still detached from its draw page.
            shapeText.setString("bullet".equals(role) ? "• " + text : text);
        }
    }

    private void checkCalcCell(Result result, XNameAccess names, Record record, String prefix) throws Exception {
        String sheetName = record.values.get(0);
        String address = record.values.get(1);
        String scalarType = record.values.get(2);
        String expected = record.values.get(3);
        String checkName = prefix + "[" + sheetName + "." + address + "]";
        if (!names.hasByName(sheetName)) {
            result.checks.add(new Check(checkName, false, "sheet is missing"));
            return;
        }
        XCell cell = cell(names, sheetName, address);
        boolean ok;
        String actual;
        if ("number".equals(scalarType)) {
            double actualNumber = cell.getValue();
            double expectedNumber = Double.parseDouble(expected);
            ok = Math.abs(actualNumber - expectedNumber) <= 1e-6;
            actual = Double.toString(actualNumber);
        } else if ("bool".equals(scalarType)) {
            double actualNumber = cell.getValue();
            double expectedNumber = Boolean.parseBoolean(expected) ? 1.0 : 0.0;
            ok = Math.abs(actualNumber - expectedNumber) <= 1e-6;
            actual = Double.toString(actualNumber);
        } else if ("null".equals(scalarType)) {
            XText text = UnoRuntime.queryInterface(XText.class, cell);
            actual = text == null ? "" : text.getString();
            ok = actual.isEmpty();
        } else {
            XText text = UnoRuntime.queryInterface(XText.class, cell);
            actual = text == null ? cell.getFormula() : text.getString();
            ok = actual.equals(expected);
        }
        result.checks.add(new Check(checkName, ok, "actual=" + actual + " expected=" + expected));
    }

    private static XCell cell(XNameAccess names, String sheetName, String address) throws Exception {
        XSpreadsheet sheet = UnoRuntime.queryInterface(XSpreadsheet.class, names.getByName(sheetName));
        if (sheet == null) {
            throw new IllegalStateException("sheet has no XSpreadsheet interface: " + sheetName);
        }
        XCell cell = UnoRuntime.queryInterface(XCell.class, sheet.getCellRangeByName(address));
        if (cell == null) {
            throw new IllegalStateException("cell has no XCell interface: " + sheetName + "." + address);
        }
        return cell;
    }

    private static void setScalar(XCell cell, String type, String value) {
        if ("number".equals(type)) {
            cell.setValue(Double.parseDouble(value));
            return;
        }
        if ("bool".equals(type)) {
            cell.setValue(Boolean.parseBoolean(value) ? 1.0 : 0.0);
            return;
        }
        XText text = UnoRuntime.queryInterface(XText.class, cell);
        if (text == null) {
            throw new IllegalStateException("cell has no XText interface");
        }
        text.setString("null".equals(type) ? "" : value);
    }

    private static XDrawPages drawPages(XComponent component) {
        XDrawPagesSupplier supplier = UnoRuntime.queryInterface(XDrawPagesSupplier.class, component);
        if (supplier == null) {
            throw new IllegalStateException("current component is not Impress");
        }
        return supplier.getDrawPages();
    }

    private static XShapes slideShapes(XDrawPages pages, int slide) throws Exception {
        if (slide < 1 || slide > pages.getCount()) {
            throw new IllegalArgumentException(
                "slide index is outside the live presentation: " + slide
            );
        }
        XShapes shapes = UnoRuntime.queryInterface(
            XShapes.class, pages.getByIndex(slide - 1)
        );
        if (shapes == null) {
            throw new IllegalStateException("slide has no XShapes interface: " + slide);
        }
        return shapes;
    }

    private static XText firstTextShape(XShapes shapes) throws Exception {
        for (int index = 0; index < shapes.getCount(); index++) {
            XText text = UnoRuntime.queryInterface(XText.class, shapes.getByIndex(index));
            if (text != null) {
                return text;
            }
        }
        return null;
    }

    private static void addImpressTextShape(
        XComponent component,
        XShapes shapes,
        String text,
        int x,
        int y,
        int width,
        int height
    ) throws Exception {
        XMultiServiceFactory factory = UnoRuntime.queryInterface(
            XMultiServiceFactory.class, component
        );
        if (factory == null) {
            throw new IllegalStateException("Impress document has no service factory");
        }
        Object shapeObject = factory.createInstance("com.sun.star.drawing.TextShape");
        XShape shape = UnoRuntime.queryInterface(XShape.class, shapeObject);
        XText shapeText = UnoRuntime.queryInterface(XText.class, shapeObject);
        if (shape == null || shapeText == null) {
            throw new IllegalStateException("Impress TextShape lacks required interfaces");
        }
        shape.setPosition(new Point(x, y));
        shape.setSize(new Size(width, height));
        shapes.add(shape);
        // Impress only retains text reliably after the new shape belongs to a page.
        shapeText.setString(text);
    }

    private static String slideText(XDrawPages pages, int slide) throws Exception {
        XShapes shapes = slideShapes(pages, slide);
        List<String> values = new ArrayList<>();
        collectText(shapes, values);
        return String.join("\n", values);
    }

    private static void collectText(XShapes shapes, List<String> values) throws Exception {
        for (int index = 0; index < shapes.getCount(); index++) {
            Object shape = shapes.getByIndex(index);
            XText text = UnoRuntime.queryInterface(XText.class, shape);
            if (text != null && text.getString() != null && !text.getString().isEmpty()) {
                values.add(text.getString());
            }
            XShapes nested = UnoRuntime.queryInterface(XShapes.class, shape);
            if (nested != null) {
                collectText(nested, values);
            }
        }
    }

    private XComponent currentComponent() {
        XComponent component = desktop.getCurrentComponent();
        if (component == null) {
            throw new IllegalStateException("LibreOffice has no current document component");
        }
        bringToFront(component);
        return component;
    }

    private static void bringToFront(XComponent component) {
        XModel model = UnoRuntime.queryInterface(XModel.class, component);
        if (model == null) {
            return;
        }
        XController controller = model.getCurrentController();
        if (controller == null) {
            return;
        }
        XFrame frame = controller.getFrame();
        if (frame == null) {
            return;
        }
        frame.activate();
        XWindow window = frame.getContainerWindow();
        if (window != null) {
            window.setVisible(true);
            window.setPosSize(0, 0, 1440, 1000, PosSize.POSSIZE);
            window.setFocus();
            XTopWindow top = UnoRuntime.queryInterface(XTopWindow.class, window);
            if (top != null) {
                top.toFront();
            }
        }
    }

    private static void closeComponent(XComponent component) {
        try {
            XCloseable closeable = UnoRuntime.queryInterface(XCloseable.class, component);
            if (closeable != null) {
                closeable.close(true);
            }
        } catch (Exception ignored) {
            // Force disposal below.  The isolated C1 document never has an
            // unsaved-close prompt at the points where this helper is called.
        }
        try {
            // Some macOS LibreOffice builds acknowledge XCloseable.close while
            // retaining the component until its remote proxy is explicitly
            // disposed.  Releasing it here makes the subsequent open a genuine
            // new live component rather than another view on the old document.
            component.dispose();
        } catch (RuntimeException ignored) {
            // A successful close can dispose the proxy before this call.
        }
    }

    private String first(String kind) {
        String value = optionalFirst(kind);
        if (value == null) {
            throw new IllegalArgumentException("protocol is missing " + kind);
        }
        return value;
    }

    private String optionalFirst(String kind) {
        for (Record record : records) {
            if (record.kind.equals(kind) && !record.values.isEmpty()) {
                return record.values.get(0);
            }
        }
        return null;
    }

    private List<Record> recordsOf(String kind) {
        List<Record> selected = new ArrayList<>();
        for (Record record : records) {
            if (record.kind.equals(kind)) {
                selected.add(record);
            }
        }
        return selected;
    }

    private static XDesktop connect(String pipeName) throws Exception {
        XComponentContext localContext = Bootstrap.createInitialComponentContext(null);
        Object resolverObject = localContext.getServiceManager().createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", localContext
        );
        XUnoUrlResolver resolver = UnoRuntime.queryInterface(XUnoUrlResolver.class, resolverObject);
        Throwable last = null;
        long deadline = System.nanoTime() + 15_000_000_000L;
        while (System.nanoTime() < deadline) {
            try {
                Object contextObject = resolver.resolve(
                    "uno:pipe,name=" + pipeName + ";urp;StarOffice.ComponentContext"
                );
                XComponentContext remoteContext = UnoRuntime.queryInterface(XComponentContext.class, contextObject);
                Object desktopObject = remoteContext.getServiceManager().createInstanceWithContext(
                    "com.sun.star.frame.Desktop", remoteContext
                );
                XDesktop desktop = UnoRuntime.queryInterface(XDesktop.class, desktopObject);
                if (desktop != null) {
                    return desktop;
                }
            } catch (Throwable error) {
                last = error;
            }
            Thread.sleep(100);
        }
        throw new IllegalStateException("could not connect to LibreOffice pipe", last);
    }

    private static List<Record> readProtocol(Path path) throws IOException {
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        if (lines.isEmpty() || !INPUT_HEADER.equals(lines.get(0))) {
            throw new IllegalArgumentException("invalid LibreOffice C1 protocol header");
        }
        List<Record> records = new ArrayList<>();
        for (int index = 1; index < lines.size(); index++) {
            String line = lines.get(index);
            if (line.isEmpty()) {
                continue;
            }
            String[] fields = line.split("\\t", -1);
            List<String> values = new ArrayList<>();
            for (int field = 1; field < fields.length; field++) {
                values.add(new String(Base64.getDecoder().decode(fields[field]), StandardCharsets.UTF_8));
            }
            records.add(new Record(fields[0], values));
        }
        return records;
    }

    private static void writeResult(Path path, Result result) throws IOException {
        List<String> lines = new ArrayList<>();
        lines.add(OUTPUT_HEADER);
        lines.add(line("STATUS", result.passed() ? "success" : "failure"));
        lines.add(line("SOURCE", SOURCE));
        if (result.documentPath != null) {
            lines.add(line("DOCUMENT_PATH", result.documentPath));
        }
        if (result.documentIdentity != null) {
            lines.add(line("DOCUMENT_IDENTITY", result.documentIdentity));
        }
        if (result.documentUrl != null) {
            lines.add(line("DOCUMENT_URL", result.documentUrl));
        }
        if (result.documentModified != null) {
            lines.add(line("DOCUMENT_MODIFIED", Boolean.toString(result.documentModified)));
        }
        if (result.sessionIdentity != null) {
            lines.add(line("SESSION_IDENTITY", result.sessionIdentity));
        }
        for (Check check : result.checks) {
            lines.add(line("CHECK", check.name, Boolean.toString(check.ok), check.detail));
        }
        for (String error : result.errors) {
            lines.add(line("ERROR", error));
        }
        Files.createDirectories(path.getParent());
        Path temporary = path.resolveSibling(path.getFileName().toString() + ".tmp");
        Files.write(temporary, lines, StandardCharsets.UTF_8);
        Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
    }

    private static String line(String kind, String... values) {
        StringBuilder builder = new StringBuilder(kind);
        for (String value : values) {
            builder.append('\t');
            builder.append(Base64.getEncoder().encodeToString(safe(value).getBytes(StandardCharsets.UTF_8)));
        }
        return builder.toString();
    }

    private static String canonicalFormula(String value) {
        return safe(value).replace("$", "").replaceAll("\\s+", "").toUpperCase(Locale.ROOT);
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }
}
