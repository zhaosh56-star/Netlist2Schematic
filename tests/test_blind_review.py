"""Blinding preserves drawings and netlists while keeping the source key private."""
import json
from pathlib import Path
from html.parser import HTMLParser
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tools.build_blind_review import anonymous_svg, group_of, script_json, render_page


class BlindReviewTests(unittest.TestCase):
    def test_device_count_boundaries_are_disjoint(self):
        expected={1:'le5',5:'le5',6:'6to10',10:'6to10',11:'11to15',15:'11to15',16:'ge16',100:'ge16'}
        self.assertEqual(expected,{n:group_of(n) for n in expected})
        for count in (0,-1,True,4.5):
            with self.subTest(count=count),self.assertRaises(ValueError):group_of(count)

    def test_netlist_cannot_close_the_payload_script(self):
        data={'netlist':'R1 a b 1k\n* </script><script>bad()</script> & Ω\n'}
        encoded=script_json(data)
        self.assertNotIn('<',encoded)
        self.assertEqual(data,json.loads(encoded))

    def test_report_constructor_does_not_end_its_own_inline_script(self):
        class Scripts(HTMLParser):
            def __init__(self):super().__init__();self.blocks=[];self.current=None
            def handle_starttag(self,tag,attrs):
                if tag=='script':self.current=[];self.blocks.append(self.current)
            def handle_endtag(self,tag):
                if tag=='script':self.current=None
            def handle_data(self,data):
                if self.current is not None:self.current.append(data)
        page=render_page('participant.template.html',{'questions':[]},'review_participant.js')
        parser=Scripts();parser.feed(page)
        self.assertEqual(4,len(parser.blocks))
        self.assertIn('window.ReviewReport', ''.join(parser.blocks[2]))
        self.assertIn('window.BlindReview', ''.join(parser.blocks[3]))

    def test_svg_source_hints_removed_and_visible_content_preserved(self):
        source='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" data-style-profile="source-secret" aria-labelledby="title">
<title id="title">source-filename GT</title><metadata>private path</metadata>
<defs><marker id="original-arrow"><path d="M0 0 L3 3"/></marker></defs>
<style>.source-class{fill:#111}</style>
<g id="source-object" class="source-class" data-component="M1" transform="translate(10 20)"><line x1="0" y1="0" x2="20" y2="0" marker-end="url(#original-arrow)"/><text x="4" y="10">M<tspan dy="3">1</tspan></text></g></svg>'''
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'drawing.svg';path.write_text(source,encoding='utf-8')
            result=anonymous_svg(path)
        text=result.decode('utf8')
        for private in ('source-secret','source-filename','private path','data-component','source-class','source-object','original-arrow','aria-labelledby'):
            self.assertNotIn(private,text)
        root=ET.fromstring(result)
        self.assertEqual('200.0',root.get('width'))
        self.assertEqual('100.0',root.get('height'))
        group=root.find('{http://www.w3.org/2000/svg}g')
        self.assertEqual('translate(10 20)',group.get('transform'))
        self.assertEqual('M1',''.join(group.find('{http://www.w3.org/2000/svg}text').itertext()))
        line=group.find('{http://www.w3.org/2000/svg}line')
        marker=root.find('.//{http://www.w3.org/2000/svg}marker')
        self.assertEqual('url(#'+marker.get('id')+')',line.get('marker-end'))


if __name__=='__main__':unittest.main()
