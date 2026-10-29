import sqlite3
import json
import sys
import re

from app.script_engine.models import ScriptFlow, ScriptStep, StepType

class TestParser:
    INTERNAL_PATTERNS = [
        r'^(?:Business|Company|Agent|Campaign|Caller|Purpose)\s*:',
        r'^(?:NURSE\s+O\'?\s*CLOCK.*INFORMATION)',
        r'^(?:Organization\s+Name|Organization\s+Type|Service\s+Location|About\s+Us|Communication\s+Style|AI\s+Receptionist\s+Role)\s*:',
        r'^(?:OFFICE\s*/\s*SERVICE\s+CENTER\s+INFORMATION|OFFICE\s+INFORMATION|SERVICE\s+CENTER)',
        r'^(?:Address|Landmark|City|State|PIN\s+Code|Phone|Email|Website|Service\s+Coverage)\s*:',
        r'^(?:DOCTOR\s+DIRECTORY|DOCTOR\s+PROFILES|DOCTORS?\s+&\s+SCHEDULES|DOCTOR\s+SCHEDULES)',
        r'^(?:SERVICES|SERVICE\s+DESCRIPTIONS)',
        r'^(?:APPOINTMENT\s*/\s*SERVICE\s+ENQUIRY)',
        r'^(?:STRICT\s+)?RECEPTIONIST\s+RULES',
        r'^(?:LEAD\s*/\s*)?APPOINTMENT\s+DETAILS\s+TO\s+COLLECT',
        r'^(?:FREQUENTLY\s+ASKED\s+QUESTIONS|FAQ|FAQS)',
        r'^(?:SOURCE\s*/\s*DATA\s+NOTE|METADATA|RAG\s+METADATA)',
        r'^(?:RULES|NOTE|IMPORTANT|INSTRUCTIONS)[\:\-]',
        r'^The\s+receptionist\s+must\s+never\s+invent',
        r'^Do\s+not\s+invent',
        r'^Role\s*:',
        r'^Availability\s*:',
        r'^Mode\s*:',
    ]

    @classmethod
    def is_internal(cls, text: str) -> bool:
        t = text.strip()
        for p in cls.INTERNAL_PATTERNS:
            if re.search(p, t, re.IGNORECASE):
                return True
        if t.startswith('Q:') or t.startswith('A:'):
            return True
        return False

    @classmethod
    def synthesize_question(cls, title: str, step_type: StepType, field: str = None) -> str:
        t_lower = title.lower()
        if step_type == StepType.GREETING or 'greet' in t_lower:
            return 'Hello! How can I assist you today?'
        if step_type == StepType.CLOSING or 'close' in t_lower or 'closing' in t_lower:
            return 'Thank you for your time. Have a wonderful day!'
        if field == 'patient_name' or 'patient name' in t_lower or 'who needs care' in t_lower:
            return "May I know the patient's name, please?"
        if field == 'caller_name' or 'caller name' in t_lower:
            return 'May I have your name, please?'
        if field == 'patient_age' or 'patient age' in t_lower or 'age' in t_lower:
            return "Could you please share the patient's age?"
        if 'service' in (field or '') or 'service' in t_lower or 'intent' in t_lower:
            return 'What healthcare service or doctor are you looking for today?'
        if 'date' in (field or '') or 'date' in t_lower:
            return 'What date and time would you prefer for the appointment?'
        if 'time' in (field or '') or 'time' in t_lower:
            return 'What time would you prefer?'
        if 'location' in (field or '') or 'locality' in t_lower or 'area' in t_lower:
            return 'Which locality or area in Chennai are you located in?'
        if 'confirm' in t_lower:
            return 'Let me confirm the information we have noted.'
        return f'Could you please provide details regarding {title.strip()}?'

    @classmethod
    def parse(cls, text: str):
        # 1. Check if composite document with explicit CONVERSATION FLOW
        flow_match = re.search(r'(?:STANDARD\s+AI\s+)?(?:RECEPTIONIST\s+)?CONVERSATION\s+FLOW\s*\n(.*?)(?=\n[A-Z0-9\s\/\:\-–]{4,}|\Z)', text, re.DOTALL | re.IGNORECASE)
        if flow_match:
            steps = []
            seq = 1
            raw_lines = [l.strip() for l in flow_match.group(1).split('\n') if l.strip()]
            field_seen = set()
            for line in raw_lines:
                m = re.match(r'^\d+[\.\:]\s*(.+)', line)
                if not m:
                    continue
                step_desc = m.group(1).strip()
                desc_lower = step_desc.lower()
                # Filter out pure internal instructions
                if any(desc_lower.startswith(p) for p in [
                    'check the authoritative', 'explain that pricing', 'for diagnosis',
                    'explain the service or doctor'
                ]):
                    continue

                # Classify step
                if 'greet' in desc_lower:
                    st = StepType.GREETING
                    field = None
                    title = 'Greeting'
                elif 'close' in desc_lower:
                    st = StepType.CLOSING
                    field = None
                    title = 'Closing'
                elif 'intent' in desc_lower or 'service' in desc_lower:
                    if 'service_required' in field_seen:
                        continue
                    st = StepType.QUESTION
                    field = 'service_required'
                    title = 'Service Requirement'
                elif 'who needs care' in desc_lower or 'patient name' in desc_lower:
                    if 'patient_name' in field_seen:
                        continue
                    st = StepType.QUESTION
                    field = 'patient_name'
                    title = 'Patient Name'
                elif 'preferred date' in desc_lower or 'date' in desc_lower:
                    if 'preferred_date' in field_seen:
                        continue
                    st = StepType.QUESTION
                    field = 'preferred_date'
                    title = 'Preferred Date and Time'
                elif 'chennai locality' in desc_lower or 'location' in desc_lower:
                    if 'location' in field_seen:
                        continue
                    st = StepType.QUESTION
                    field = 'location'
                    title = 'Location'
                elif 'confirm' in desc_lower:
                    if 'confirmation' in field_seen:
                        continue
                    st = StepType.QUESTION
                    field = 'confirmation'
                    title = 'Confirmation'
                else:
                    continue

                if field:
                    field_seen.add(field)

                q_text = cls.synthesize_question(title, st, field)
                steps.append(ScriptStep(
                    step_id=f'step_{seq:03d}',
                    sequence=seq,
                    type=st,
                    title=title,
                    objective=f'Accomplish {title}',
                    question_or_text=q_text,
                    expected_field=field,
                    speakable=True
                ))
                seq += 1

            # Insert patient_age if patient_name exists and age not yet added
            has_name = any(s.expected_field == 'patient_name' for s in steps)
            has_age = any(s.expected_field == 'patient_age' for s in steps)
            if has_name and not has_age:
                idx = next(i for i, s in enumerate(steps) if s.expected_field == 'patient_name')
                age_step = ScriptStep(
                    step_id='step_temp',
                    sequence=0,
                    type=StepType.QUESTION,
                    title='Patient Age',
                    objective='Accomplish Patient Age',
                    question_or_text=cls.synthesize_question('Patient Age', StepType.QUESTION, 'patient_age'),
                    expected_field='patient_age',
                    speakable=True
                )
                steps.insert(idx + 1, age_step)
                for i, s in enumerate(steps):
                    s.sequence = i + 1
                    s.step_id = f'step_{i+1:03d}'

            for i in range(len(steps) - 1):
                steps[i].next_step_id = steps[i+1].step_id

            return steps

if __name__ == "__main__":
    con = sqlite3.connect('callzenza.db')
    cur = con.cursor()
    row = cur.execute('SELECT schedule FROM campaigns LIMIT 1').fetchone()
    data = json.loads(row[0])
    script_text = data.get('script')

    steps = TestParser.parse(script_text)
    print('TOTAL CLEAN STEPS:', len(steps))
    for s in steps:
        print(f'{s.step_id} | {s.type} | {s.title} | field={s.expected_field}')
        print(f'   question: "{s.question_or_text}"')
