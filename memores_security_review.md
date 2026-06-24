### [HIGH] - Primary Key Exposure in Create Serializer Enables IDOR/Mass Assignment
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/serializers/course_serializers.py`
- **Vulnerability Type:** Mass Assignment / Broken Object Level Authorization (BOLA)
- **Evidence:**
```python
{'absolute_path': '/Users/dansparkes/memores/memores-api/memores/serializers/course_serializers.py', 'relative_path': 'memores/serializers/course_serializers.py', 'class': 'CourseCreateSerializer', 'fields': ['id', 'course_type']}
```

### [MEDIUM] - Empty Serializer Field Definition Indicates Misconfiguration or Data Leakage
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/serializers/course_serializers.py`
- **Vulnerability Type:** Serialization Misconfiguration / API Contract Violation
- **Evidence:**
```python
{'absolute_path': '/Users/dansparkes/memores/memores-api/memores/serializers/course_serializers.py', 'relative_path': 'memores/serializers/course_serializers.py', 'class': 'QuestionSerializer', 'fields': []}
```

### [HIGH] - Destructive Administrative Endpoints Lack Visible Permission Scoping
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/admin/content.py`
- **Vulnerability Type:** Broken Access Control / Privilege Escalation
- **Evidence:**
```python
{'absolute_path': '/Users/dansparkes/memores/memores-api/memores/views/admin/content.py', 'relative_path': 'memores/views/admin/content.py', 'class': 'AdminUserCompletedCourseDestroyView', 'methods': ['destroy', 'perform_destroy']}
```

### [MEDIUM] - Unscoped User Update Endpoint Risks Mass Assignment
- **Target File:** `/Users/dansparkes/memores/memores-api/memores/views/app/user.py`
- **Vulnerability Type:** Mass Assignment / Privilege Escalation
- **Evidence:**
```python
{'absolute_path': '/Users/dansparkes/memores/memores-api/memores/views/app/user.py', 'relative_path': 'memores/views/app/user.py', 'class': 'UserView', 'methods': ['get', 'patch', 'post']}
```
