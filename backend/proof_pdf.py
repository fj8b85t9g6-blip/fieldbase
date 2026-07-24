"""Generate a compact, dispute-ready job proof package."""

from io import BytesIO
import logging

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)


def _text(value, fallback="Not recorded"):
    value = str(value or "").strip()
    return value or fallback


def _scaled_image(image_bytes, max_width=3.15 * inch, max_height=2.25 * inch):
    reader = ImageReader(BytesIO(image_bytes))
    width, height = reader.getSize()
    scale = min(max_width / width, max_height / height)
    return Image(BytesIO(image_bytes), width=width * scale, height=height * scale)


def build_proof_package(
    company,
    job,
    changes,
    photos,
    documents,
    checklist,
    profit,
    photo_assets=None,
    signature_bytes=None,
):
    """Return a polished PDF containing the durable evidence for one job."""
    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=letter,
        rightMargin=0.55 * inch,
        leftMargin=0.55 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title=f"Job Proof Package - {job.title}",
        author=company.name,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ProofTitle",
        parent=styles["Title"],
        textColor=colors.HexColor("#17324f"),
        fontSize=22,
        leading=27,
        alignment=TA_CENTER,
        spaceAfter=8,
    ))
    styles.add(ParagraphStyle(
        name="Section",
        parent=styles["Heading2"],
        textColor=colors.HexColor("#17324f"),
        fontSize=13,
        leading=17,
        spaceBefore=10,
        spaceAfter=7,
    ))
    styles.add(ParagraphStyle(
        name="Small",
        parent=styles["BodyText"],
        textColor=colors.HexColor("#64748b"),
        fontSize=8.5,
        leading=11,
    ))
    styles["BodyText"].fontSize = 9.5
    styles["BodyText"].leading = 13

    story = [
        Paragraph("FIELD-VERIFIED JOB RECORD", styles["ProofTitle"]),
        Paragraph(_text(company.name), ParagraphStyle(
            "Company",
            parent=styles["BodyText"],
            alignment=TA_CENTER,
            textColor=colors.HexColor("#475569"),
            spaceAfter=14,
        )),
    ]

    summary = [
        ["Job", _text(job.title), "Status", _text(job.status).replace("_", " ").title()],
        ["Client", _text(job.client_company or job.client_name), "Technician", _text(job.tech_assigned)],
        ["Location", _text(job.location), "Platform", _text(job.platform).title()],
        ["Scheduled", job.start_time.strftime("%b %d, %Y %I:%M %p"), "Job ID", str(job.id)],
    ]
    table = Table(summary, colWidths=[0.9 * inch, 2.45 * inch, 0.8 * inch, 2.55 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce3ec")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#172033")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 10)])

    story.extend([
        Paragraph("Original Locked Scope", styles["Section"]),
        Paragraph(_text(job.original_scope or job.notes), styles["BodyText"]),
        Paragraph(
            f"Scope locked: {job.scope_locked_at.strftime('%b %d, %Y %I:%M %p') if job.scope_locked_at else 'Not locked'}",
            styles["Small"],
        ),
    ])

    story.append(Paragraph("Approved Change Orders", styles["Section"]))
    if changes:
        rows = [["Status", "Description", "Amount", "Approved"]]
        for change, data in changes:
            rows.append([
                _text(change.status).title(),
                _text(data.get("description") or change.title),
                f"${float(change.amount or 0):,.2f}",
                change.approved_at.strftime("%b %d, %Y") if change.approved_at else "-",
            ])
        changes_table = Table(rows, colWidths=[0.75 * inch, 3.65 * inch, 0.9 * inch, 1.3 * inch], repeatRows=1)
        changes_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#dce3ec")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(changes_table)
    else:
        story.append(Paragraph("No change orders were recorded.", styles["BodyText"]))

    timeline = [
        ["Confirmed", job.confirmed_at],
        ["Clocked in", job.clock_in_at],
        ["Clocked out", job.clock_out_at],
        ["Completed", job.completed_at],
        ["Customer signed", job.signed_at],
    ]
    story.append(Paragraph("Field Timeline", styles["Section"]))
    timeline_rows = [["Event", "Timestamp"]] + [
        [label, value.strftime("%b %d, %Y %I:%M:%S %p") if value else "Not recorded"]
        for label, value in timeline
    ]
    timeline_table = Table(timeline_rows, colWidths=[2.0 * inch, 4.6 * inch], repeatRows=1)
    timeline_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf2ff")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17324f")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#dce3ec")),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(timeline_table)

    story.append(Paragraph("Closeout Evidence", styles["Section"]))
    closeout_lines = []
    for item in checklist:
        closeout_lines.append(
            Paragraph(
                f"{'COMPLETE' if item.get('done') else 'INCOMPLETE'} - {_text(item.get('label'))}",
                styles["BodyText"],
            )
        )
    if not closeout_lines:
        closeout_lines.append(Paragraph("No checklist items were configured.", styles["BodyText"]))
    signature_line = (
        f"Customer signature: {job.signature_name} on {job.signed_at.strftime('%b %d, %Y %I:%M %p')}"
        if job.signed_at else "Customer signature: Not recorded"
    )
    story.append(KeepTogether(closeout_lines + [Spacer(1, 5), Paragraph(signature_line, styles["BodyText"])]))
    if signature_bytes:
        try:
            story.extend([
                Spacer(1, 5),
                _scaled_image(signature_bytes, max_width=3.0 * inch, max_height=1.1 * inch),
            ])
        except Exception:
            story.append(Paragraph("Signature image could not be rendered.", styles["Small"]))

    story.append(PageBreak())
    story.append(Paragraph("Attachments", styles["Section"]))
    story.append(Paragraph(
        f"{len(photos)} job photo(s) and {len(documents)} document(s) recorded.",
        styles["BodyText"],
    ))
    for photo in photos:
        story.append(Paragraph(
            f"Photo {photo.id}: {photo.filename} - uploaded {photo.uploaded_at.strftime('%b %d, %Y %I:%M %p')}",
            styles["Small"],
        ))
    for document in documents:
        story.append(Paragraph(
            f"Document {document.id}: {_text(document.original_name or document.filename)} - uploaded {document.uploaded_at.strftime('%b %d, %Y %I:%M %p')}",
            styles["Small"],
        ))
    if photo_assets:
        story.append(Spacer(1, 8))
        photo_cells = []
        for photo, image_bytes in photo_assets[:8]:
            try:
                photo_cells.append([
                    _scaled_image(image_bytes),
                    Paragraph(
                        f"Photo {photo.id} - {photo.uploaded_at.strftime('%b %d, %Y %I:%M %p')}",
                        styles["Small"],
                    ),
                ])
            except Exception as exc:
                logger.warning('Could not render job photo %s in proof package: %s', photo.id, exc)
                continue
        rows = [photo_cells[index:index + 2] for index in range(0, len(photo_cells), 2)]
        if rows:
            if len(rows[-1]) == 1:
                rows[-1].append("")
            photo_table = Table(rows, colWidths=[3.35 * inch, 3.35 * inch])
            photo_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            story.append(photo_table)

    story.append(PageBreak())
    story.append(Paragraph("Job Economics", styles["Section"]))
    economics = [
        ["Client revenue", f"${profit['revenue']:,.2f}"],
        ["Technician pay", f"${profit['tech_pay']:,.2f}"],
        ["Materials", f"${profit['materials']:,.2f}"],
        ["Platform fees", f"${profit['platform_fees']:,.2f}"],
        ["Travel and other costs", f"${profit['other_costs']:,.2f}"],
        ["Estimated profit", f"${profit['profit']:,.2f}"],
        ["Effective hourly rate", f"${profit['effective_hourly']:,.2f}/hr"],
    ]
    economics_table = Table(economics, colWidths=[3.4 * inch, 2.1 * inch])
    economics_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#dce3ec")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([
        economics_table,
        Spacer(1, 14),
        Paragraph("Technician Notes", styles["Section"]),
        Paragraph(_text(job.employee_notes), styles["BodyText"]),
        Spacer(1, 22),
        Paragraph(
            "Generated by FieldBase. This package summarizes records stored for the job and does not replace the governing contract.",
            styles["Small"],
        ),
    ])

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#dce3ec"))
        canvas.line(0.55 * inch, 0.38 * inch, 7.95 * inch, 0.38 * inch)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(0.55 * inch, 0.22 * inch, f"FieldBase proof package - Job {job.id}")
        canvas.drawRightString(7.95 * inch, 0.22 * inch, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
