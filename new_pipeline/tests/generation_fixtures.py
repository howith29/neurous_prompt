def output(level):
    paragraphs={'BEGINNER':1,'INTERMEDIATE':2,'ADVANCED':3}[level]
    length=50 if level!='ADVANCED' else 60
    sentence='가'*(length-1)+'.'
    return '제목: '+level+'\n\n'+'\n\n'.join(' '.join([sentence]*4) for _ in range(paragraphs))
