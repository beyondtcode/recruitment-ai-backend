import os
import shutil
import re

# הנתיב המדויק לתיקיית הקבצים שלך
source_folder = r"C:\Users\user\Documents\recruitment-ai\recruitment-ai-backend\test_cvs"
failed_folder = os.path.join(source_folder, "failed_uploads")

# יצירת תיקיית "failed_uploads" אם היא לא קיימת
os.makedirs(failed_folder, exist_ok=True)

# הלוג המלא שסיפקת
log_text = """
ERROR processing 6412 - אלישבע לינצ_נר - 15867135._.pdf: Error code: 400
ERROR processing 6413 - אילה יגלניק - 15867788._.pdf: Error code: 400
ERROR processing 6414 - מרים תיק - 15870659.pdf: Error code: 400
ERROR processing 6415 - שרונה גלילי - 15871178.doc.docx: Error code: 400
ERROR processing 6417 - ענת בן שיה - 15875372.doc.docx: Error code: 400
ERROR processing 6418 - תמר דנינו - 15877636.pdf: Error code: 400
ERROR processing 6420 - Adi Fayer - 15885870.pdf: Error code: 400
ERROR processing 6421 - Shachar Dahari - 15885924.pdf: Error code: 400
ERROR processing 6422 - יעל בן שושן - 15886848.pdf: No text could be extracted from the PDF.
ERROR processing 6423 - Esther Halperin - 15887307.pdf: Error code: 400
ERROR processing 6424 - Talya Kazayof - 15887338.docx: Error code: 400
ERROR processing 6425 -   - 15887369.pdf: Error code: 400
ERROR processing 6426 -   - 15887430.pdf: Error code: 400
ERROR processing 6427 - חיה שפרונג - 15887441.pdf: Error code: 400
ERROR processing 6428 - Tamar Glass - 15887535.pdf: Error code: 400
ERROR processing 6429 - Tamar Dinavetsky - 15887536.pdf: Error code: 400
ERROR processing 6430 -   - 15887609.pdf: Error code: 400
ERROR processing 6431 - Shahar Itach - 15887610.pdf: Error code: 400
ERROR processing 6432 - Ayala Barsheshet - 15900591.pdf: Error code: 400
ERROR processing 6433 - Yosef Haimjan - 15887683.docx: Error code: 400
ERROR processing 6434 - Haya Kaabeya - 15887798.pdf: Error code: 400
ERROR processing 6435 - אסתר יעקבזון - 15887938.pdf: Error code: 400
ERROR processing 6436 - Batya Zilberberg - 15887939.pdf: Error code: 400
ERROR processing 6437 - Yarden Abergel - 15887972.pdf: Error code: 400
ERROR processing 6438 - Tal Zigelnik - 15888025.pdf: Error code: 400
ERROR processing 6439 - Guy Pariente - 15888026.pdf: Error code: 400
ERROR processing 6440 -   - 15888204.docx: Error code: 400
ERROR processing 6441 - Tamar Min Hahar - 15888251.pdf: Error code: 400
ERROR processing 6442 - שרה פרקש - 15888289.docx: Error code: 400
ERROR processing 6443 - Nir Geron - 16032244.pdf: Error code: 400
ERROR processing 6444 - Omer Dayan - 15888768.pdf: Error code: 400
ERROR processing 6445 - רחל לב - 16006635.pdf: Error code: 400
ERROR processing 6446 - Shon Grinberg - 15889091.pdf: Error code: 400
ERROR processing 6447 - מיכל עקב - 15889193.docx: Error code: 400
ERROR processing 6448 - Guy Baruch - 15889301.pdf: Error code: 400
ERROR processing 6449 - Stav Zysblatt - 15889609.pdf: Error code: 400
ERROR processing 6450 - Linoy Vaizman - 15889645.pdf: Error code: 400
ERROR processing 6452 - Chagit Levi - 15889841.pdf: Error code: 400
ERROR processing 6453 - Rotem Tal - 15889864.pdf: Error code: 400
ERROR processing 6454 - Elisheva Katzinelbogen - 15889994.pdf: Error code: 400
ERROR processing 6455 - Efrat Katzenelbogen - 15890014.pdf: Error code: 400
ERROR processing 6456 -   - 15890147.pdf: Error code: 400
ERROR processing 6457 - Golda Yodelwich - 15890267.pdf: Error code: 400
ERROR processing 6458 - Jack (Yaron) Yakov Amichai - 15890373.pdf: Error code: 400
ERROR processing 6459 - Aviv Ben Kimon - 15890431.pdf: Error code: 400
ERROR processing 6460 - Adiel Bulganim - 16035586.pdf: Error code: 400
ERROR processing 6461 - Amit Malka - 16031922.pdf: Error code: 400
ERROR processing 6462 -   - 15890568.pdf: Error code: 400
ERROR processing 6463 - Ori Perelman - 15890641.pdf: Error code: 400
ERROR processing 6464 - Adam Kassem - 15890618.pdf: Error code: 400
ERROR processing 6465 - Daniel Avitsror - 15890772.pdf: Error code: 400
ERROR processing 6466 - נעמה רוב - 15890984.pdf: Error code: 400
ERROR processing 6467 - Ilan Korol - 15891042.pdf: Error code: 400
ERROR processing 6468 - Oshrat Cohen - 15891138.pdf: Error code: 400
ERROR processing 6469 - Doron Yom Tov - 15891203.pdf: Error code: 400
ERROR processing 6470 - Christine Khalil - 15891287.pdf: Error code: 400
ERROR processing 6472 - Yuval Siton - 15891402.pdf: Error code: 400
ERROR processing 6473 - איתי סלומון - 15891571.docx: Error code: 400
ERROR processing 6475 - Roza Bass - 15892121.pdf: Error code: 400
ERROR processing 6476 - Eyal Miedzinski - 15892196.pdf: Error code: 400
ERROR processing 6477 -   - 15892228.pdf: Error code: 400
ERROR processing 6478 -   - 15892305.pdf: Error code: 400
ERROR processing 6479 -   - 15892310.pdf: Error code: 400
ERROR processing 6480 - חנה ישראלי - 15892318.pdf: Error code: 400
ERROR processing 6481 - Tzahi Tahan-Ai Builder - 15892363.pdf: Error code: 400
ERROR processing 6482 -   - 15892440.pdf: No text could be extracted from the PDF.
ERROR processing 6483 - Suleiman Awiwi - 15892466.pdf: Error code: 400
ERROR processing 6484 -   - 15892889.pdf: Error code: 400
ERROR processing 6485 - Irad Yaacoby - 15893041.pdf: Error code: 400
ERROR processing 6486 -   - 15893131.pdf: Error code: 400
ERROR processing 6487 - Shir Mamia - 15893613.docx: Error code: 400
ERROR processing 6488 - שי יפת - 15893742.pdf: Error code: 400
ERROR processing 6489 - Kfir Tayar - 15893796.pdf: Error code: 400
ERROR processing 6490 - Gal Presil - 15893872.pdf: Error code: 400
ERROR processing 6491 -   - 15894066.docx: Error code: 400
ERROR processing 6492 - Lidor Pahima - 15894205.pdf: Error code: 400
ERROR processing 6493 - Dayana Pergament - 15894334.docx: Error code: 400
ERROR processing 6494 - Sivan Zargari - 15894300.pdf: Error code: 400
ERROR processing 6495 -   - 15894350.pdf: Error code: 400
ERROR processing 6496 - Ori Baruch - 15894370.pdf: Error code: 400
ERROR processing 6497 -   - 15894557.pdf: Error code: 400
ERROR processing 6498 - חנה הלפרין - 15894998.pdf: Error code: 400
ERROR processing 6499 - Haim Ishta - 15895009.pdf: Error code: 400
ERROR processing 6500 - Kobi Bentata - 15895010.pdf: Error code: 400
ERROR processing 6501 -   - 15895079.pdf: Error code: 400
ERROR processing 6502 - Dan Dorfman - 15895207.pdf: Error code: 400
ERROR processing 6503 - Chaya Zak - 15895378.pdf: Error code: 400
ERROR processing 6504 - Almog David - 16029050.pdf: Error code: 400
ERROR processing 6505 - Eyal Abisdris - 15895484.pdf: Error code: 400
ERROR processing 6506 - Omri Shema - 15896096.pdf: Error code: 400
ERROR processing 6507 - ליעד טרבלסי - 15896264.pdf: Error code: 400
ERROR processing 6509 -   - 15896516.pdf: Error code: 400
ERROR processing 6510 - Dori Rosen - 15896753.pdf: Error code: 400
ERROR processing 6511 - Ronen Shilchikov - 15897164.pdf: Error code: 400
ERROR processing 6512 - Maya Sasson - 15897551.pdf: Error code: 400
ERROR processing 6513 - Yousef Rohana - 16029186.pdf: Error code: 400
ERROR processing 6514 - Sarah Schachter - 15898177.pdf: Error code: 400
ERROR processing 6515 - Rivka Sternbuch - 15898419.pdf: Error code: 400
ERROR processing 6516 - שרה מיטלמן - 16024583.pdf: Error code: 400
ERROR processing 6517 - Shirel Orkabi - 16040842.pdf: Error code: 400
ERROR processing 6518 - Miri Koren - 15899965.pdf: Error code: 400
ERROR processing 6519 - Ilan Soussan - 15900038.pdf: Error code: 400
ERROR processing 6520 - Roi Dolev - 15900117.pdf: Error code: 400
ERROR processing 6521 - דוד אלחנדרו ממן - 15900173.pdf: Error code: 400
ERROR processing 6522 - Noam Mazuz - 15901029.pdf: Error code: 400
ERROR processing 6523 - אפרת שלום - 15901863.docx: Error code: 400
ERROR processing 6525 - Eliav Yair - 15902635.docx: Error code: 400
ERROR processing 6526 - Tehilla Sher - 15904016.pdf: Error code: 400
ERROR processing 6529 - תמר נחמה מילר - 15904516.pdf: Error code: 400
ERROR processing 6530 - אגם אוטמזגין - 15904625.pdf: Error code: 400
ERROR processing 6531 - Itamar Atia - 15904994.pdf: Error code: 400
ERROR processing 6533 - מרים סלומון - 15905431.pdf: Error code: 400
ERROR processing 6534 -   - 15906077.pdf: Error code: 400
ERROR processing 6536 - Yoad Kotkovski - 15906333.docx: Error code: 400
ERROR processing 6537 - Imri Fichman - 15907347.pdf: Error code: 400
ERROR processing 6539 -   - 15907804.pdf: Error code: 400
ERROR processing 6540 - Yuval Vogdan - 16035944.pdf: Error code: 400
ERROR processing 6561 - Eliya Shainberg - 15908448.pdf: Error code: 400
ERROR processing 6562 - Joshua Levi - 15909351.pdf: Error code: 400
ERROR processing 6563 - Yasmin Adler - 15909497.pdf: Error code: 400
ERROR processing 6565 -   - 15911791.docx: Error code: 400
ERROR processing 6567 - Shani Gavra - 15916720.pdf: Error code: 400
ERROR processing 6580 -   - 15922057.pdf: Error code: 400
ERROR processing 6581 -   - 15922104.pdf: Error code: 400
ERROR processing 6583 - מיכל שרה בוטח - 15928950.pdf: Error code: 400
ERROR processing 6586 - Ohad Hirsh - 15933673.docx: Error code: 400
ERROR processing 6588 - תמר נוגריאן - 15937380.pdf: Error code: 400
ERROR processing 6589 - נתניאל בן גרא - 15942617.pdf: Error code: 400
ERROR processing 6590 -   - 15947254._.pdf: No text could be extracted from the PDF.
ERROR processing 6591 - Efrat Marciano - 16015320.pdf: Error code: 400
ERROR processing 6592 - Tamar Levi - 15949324.pdf: Error code: 400
ERROR processing 6596 - Tsivya Cohen - 15954038.pdf: Error code: 400
ERROR processing 6597 -   - 15957310.pdf: Error code: 400
ERROR processing 6611 - Sammy Schaechter - 15972535.pdf: Error code: 400
ERROR processing 6613 - Hagit Arama - 15976451.docx: Error code: 400
ERROR processing 6615 - פולינה בבינסקי - 15980416.docx: Error code: 400
ERROR processing 6628 - נעמה פוקסברומר - 15987786._.pdf: Error code: 400
ERROR processing 6629 - שחר הודיה דאבוש - 15991762.pdf: Error code: 400
ERROR processing 6630 - חנה רבקה לוי - 15991776.docx: Error code: 400
ERROR processing 6631 - Esther Schwartz - 15991951.docx: Error code: 400
ERROR processing 6633 - מלי גולדברג-מפתחת אוטומציה - 16009528.pdf: Error code: 400
ERROR processing 6634 - מרים אליאך - 15992232.docx: Error code: 400
ERROR processing 6635 - Hodaya Cohen - 15992300.pdf: Error code: 400
ERROR processing 6636 -   - 15992466.pdf: Error code: 400
ERROR processing 6637 - Chava Paperman-Bamberger - 15992554.docx: Error code: 400
ERROR processing 6638 - Devorah Young - 15992886.pdf: Error code: 400
ERROR processing 6639 -   - 15994454.docx: Error code: 400
ERROR processing 6640 -   - 15994492.docx: Error code: 400
ERROR processing 6641 - שני פרידמן - 15994600.pdf: Error code: 400
ERROR processing 6642 - Peri Kuflik - 15994601.docx: Error code: 400
ERROR processing 6643 - רבקה לודמיר - 15994791._.pdf: Error code: 400
ERROR processing 6644 - מוריה טולדנו - 15995914.pdf: Error code: 400
ERROR processing 6645 - דינה גולדברג - 15996310._.pdf: Error code: 400
ERROR processing 6646 -   - 15996993.docx: Error code: 400
ERROR processing 6647 -   - 15997092.pdf: Error code: 400
ERROR processing 6648 - הני טאוב - 15997245.pdf: Error code: 400
ERROR processing 6654 -   - 15997882.pdf: Error code: 400
ERROR processing 6655 - נעמי טהרני - 15998993.docx: Error code: 400
ERROR processing 6656 - תהילה רוזן - 15999078.pdf: Error code: 400
ERROR processing 6657 - רבקה זבדי - 16006238.docx: Error code: 400
ERROR processing 6658 -   - 16006335.pdf: Error code: 400
ERROR processing 6659 - חוה לוי - 16007829.pdf: Error code: 400
ERROR processing 6660 - Dan Ben-Zikry - 16008544._.pdf: Error code: 400
ERROR processing 6663 -   - 16012490.docx: Error code: 400
ERROR processing 6667 - איילת שפירו - 16021726._.pdf: Error code: 400
ERROR processing 6668 -   - 16024395.pdf: Error code: 400
ERROR processing 6677 - Omer Erez - 16030126.pdf: Error code: 400
ERROR processing 6678 - Tomer Yaakov Kahanowich - 16030870.pdf: Error code: 400
ERROR processing 6680 - Lidor Amraby - 16031072.pdf: Error code: 400
ERROR processing 6681 - Itay Maoz - 16032388.pdf: Error code: 400
ERROR processing 6682 - Nave Sfunim - 16032770.pdf: Error code: 400
ERROR processing 6683 - Lotem Cohen - 16032959.pdf: Error code: 400
ERROR processing 6684 - Hadas Hadad - 16034278.pdf: Error code: 400
ERROR processing 6685 -   - 16035066.pdf: Error code: 400
ERROR processing 6686 - Waseem Somre - 16035184.pdf: Error code: 400
ERROR processing 6687 - גל מזרחי - 16035630.docx: Error code: 400
ERROR processing 6688 - Yaron Gefen - 16038142.pdf: Error code: 400
ERROR processing 6689 - Danny Avraham Shore - 16038431.pdf: Error code: 400
ERROR processing 6691 - Sarah Schwartz - 16040208.pdf: Error code: 400
ERROR processing 6692 - Noam Diamant - 16040346.pdf: Error code: 400
ERROR processing 676 - Gila Leiman - 13808977.docx: Error code: 400
ERROR processing 678 - אסתר בלס - 15750167.pdf: Error code: 400
ERROR processing 692 - טלי אלול - 12959791.docx: Error code: 400
ERROR processing 860 - אמונה לוי - 13775306.pdf: Error code: 400
ERROR processing 873 - נעמי קירזון - 15887465.pdf: Error code: 400
ERROR processing 876 - Yael Ajami - 15465774.pdf: Error code: 400
ERROR processing 920 -   - 13176715.pdf: Error code: 400
ERROR processing 941 - Rachel Lubinsky - 15992762.pdf: Error code: 400
ERROR processing 952 - שולמית קולין - 13987591.docx: Error code: 400
"""

# חילוץ שמות הקבצים מתוך שורות השגיאה
filenames = re.findall(r"ERROR processing (.*?):", log_text)

moved_count = 0
for filename in filenames:
    filename = filename.strip()
    src_path = os.path.join(source_folder, filename)
    dst_path = os.path.join(failed_folder, filename)
    
    # בדיקה אם הקובץ קיים בתיקייה המקורית והעברה שלו
    if os.path.exists(src_path):
        shutil.move(src_path, dst_path)
        moved_count += 1
        print(f"הועבר בהצלחה: {filename}")

print(f"\nסיום! {moved_count} קבצים שנכשלו הועברו לתיקיית 'failed_uploads'.")
print("הקבצים שנשארו בתיקייה המקורית הם אלו שהועלו בהצלחה.")